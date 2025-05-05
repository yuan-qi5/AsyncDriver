import os
import json
import argparse
from tqdm import tqdm
import pickle
from llama2.utils.common_utils import *
from llama2.utils.data_utils import *
from gameformer.data_utils import *
import matplotlib.pyplot as plt
from nuplan.planning.utils.multithreading.worker_parallel import SingleMachineParallelExecutor
from nuplan.planning.scenario_builder.scenario_filter import ScenarioFilter
from nuplan.planning.scenario_builder.nuplan_db.nuplan_scenario_builder import NuPlanScenarioBuilder
from nuplan.planning.scenario_builder.nuplan_db.nuplan_scenario_utils import ScenarioMapping
from nuplan.common.actor_state.state_representation import Point2D

import multiprocessing

import warnings
warnings.filterwarnings("ignore") # 用于忽略所有警告信息，以保持日志或控制台输出简洁

class DirectionDecision:
    GO_STRAIGHT = 'go_straight'
    TURN_LEFT = 'turn_left'
    TURN_RIGHT = 'turn_right'
    
# define data processor
class DataProcessor(object):
    def __init__(self, scenarios):
        self._scenarios = scenarios

        self.past_time_horizon = 2 # [seconds] 向前回溯时间长度
        self.num_past_poses = 10 * self.past_time_horizon # 回溯轨迹点数，采样率为 10 HZ
        self.future_time_horizon = 8 # [seconds] 向后预测时间长度
        self.num_future_poses = 10 * self.future_time_horizon # 向后预测点数，采样率为 10 HZ
        self.num_agents = 20

        self._map_features = ['LANE', 'ROUTE_LANES', 'CROSSWALK'] # name of map features to be extracted.
        self._max_elements = {'LANE': 40, 'ROUTE_LANES': 10, 'CROSSWALK': 5} # maximum number of elements to extract per feature layer.
        self._max_points = {'LANE': 50, 'ROUTE_LANES': 50, 'CROSSWALK': 30} # maximum number of points per feature to extract per feature layer.
        self._radius = 60 # [m] query radius scope relative to the current pose.
        self._interpolation_method = 'linear' # Interpolation method to apply when interpolating to maintain fixed size map elements.

    # 返回过去到现在的状态 tensor，时间戳 tensor
    def get_ego_agent(self, iteration=None):
        # self.anchor_ego_state = self.scenario.initial_ego_state
        self.anchor_ego_state = self.current_ego_state
        
        past_ego_states = self.scenario.get_ego_past_trajectory(
            iteration=iteration, num_samples=self.num_past_poses, time_horizon=self.past_time_horizon
        )
        sampled_past_ego_states = list(past_ego_states) + [self.anchor_ego_state]
        past_ego_states_tensor = sampled_past_ego_states_to_tensor(sampled_past_ego_states)

        past_time_stamps = list(
            self.scenario.get_past_timestamps(
                iteration=iteration, num_samples=self.num_past_poses, time_horizon=self.past_time_horizon
            )
        ) + [self.scenario.get_time_point(iteration=iteration)]
        # ) + [self.scenario.start_time]

        past_time_stamps_tensor = sampled_past_timestamps_to_tensor(past_time_stamps)

        return past_ego_states_tensor, past_time_stamps_tensor

    # 返回 ：按时间顺序的邻居实体特征张量列表和按时间顺序的邻居实体类型列表
    def get_neighbor_agents(self, iteration=None):
        # present_tracked_objects = self.scenario.initial_tracked_objects.tracked_objects
        # 返回指定 iteration 的跟踪数据对象，.tracked_objects 是其中保存的当前所有 agnet 的数据
        present_tracked_objects = self.scenario.get_tracked_objects_at_iteration(iteration=iteration).tracked_objects
        # 返回每个过去时刻的 .tracked_objects 列表，得到历史帧对象
        past_tracked_objects = [
            tracked_objects.tracked_objects
            for tracked_objects in self.scenario.get_past_tracked_objects(
                iteration=iteration, time_horizon=self.past_time_horizon, num_samples=self.num_past_poses
            )
        ]

        sampled_past_observations = past_tracked_objects + [present_tracked_objects]
        # \ 是 Python 的行连接符（续行符）：让一行代码在视觉上分成多行书写，但逻辑上仍是一行代码
        past_tracked_objects_tensor_list, past_tracked_objects_types = \
              sampled_tracked_objects_to_tensor_list(sampled_past_observations) 

        return past_tracked_objects_tensor_list, past_tracked_objects_types
    
    # 返回一个向量化地图表示 
    def get_map(self, iteration=None): 
        # ego_state = self.scenario.initial_ego_state  
        ego_state = self.current_ego_state    
        ego_coords = Point2D(ego_state.rear_axle.x, ego_state.rear_axle.y)
        # 获取当前规划路径上的 “路径 ID”，可对地图要素进行语义筛选，以忽略与当前车辆路线无关的路段，减少冗余
        route_roadblock_ids = self.scenario.get_route_roadblock_ids() 
        traffic_light_data = self.scenario.get_traffic_light_status_at_iteration(iteration)

        coords, traffic_light_data = get_neighbor_vector_set_map(
            self.map_api, self._map_features, ego_coords, self._radius, route_roadblock_ids, traffic_light_data
        )

        vector_map = map_process(ego_state.rear_axle, coords, traffic_light_data, self._map_features, 
                                 self._max_elements, self._max_points, self._interpolation_method)

        return vector_map

    # 返回未来的相对轨迹点
    def get_ego_agent_future(self,iteration=None):
        # current_absolute_state = self.scenario.initial_ego_state
        current_absolute_state = self.current_ego_state

        trajectory_absolute_states = self.scenario.get_ego_future_trajectory(
            iteration=iteration, num_samples=self.num_future_poses, time_horizon=self.future_time_horizon
        )

        # Get all future poses of the ego relative to the ego coordinate system
        trajectory_relative_poses = convert_absolute_to_relative_poses(
            current_absolute_state.rear_axle, [state.rear_axle for state in trajectory_absolute_states]
        )

        return trajectory_relative_poses

    # 返回 命令列表和对应距离列表拼成的列表、指令（option）
    def get_instruction(self, ego_future_poses, threshold=0.5, return_prompt=False):
        dis_norm = np.linalg.norm(np.diff(ego_future_poses[:,:-1], n=1, axis=0), axis=1)
        dis_cum = np.cumsum(dis_norm, axis=0)
        
        cur_cmd = None
        cur_dis = 0
        instruction = ''
        cmd_ls = [] # 存访命令序列
        dis_ls = [] # 存访对应每条命令的距离
        tmp_dis_ls = []
        time_ls = []
        for heading, (idx, dis), d_n in zip(ego_future_poses[1:,2], enumerate(dis_cum), dis_norm):
            if heading > threshold :
                cmd = 'turn right in '
            elif heading < -threshold:
                cmd = 'turn left in '
            elif d_n>0.1:
                cmd = 'go straight in '
            else:
                cmd = 'stop. '
            if cur_cmd == None:
                cur_cmd = cmd
            elif cmd!=cur_cmd:
                cmd_ls.append(cur_cmd)
                dis_ls.append(np.round(dis_cum[idx-1]-cur_dis, 2))
                if 'stop' not in cur_cmd:
                    cur_dis = dis_cum[idx-1]
                cur_cmd = cmd

        # 收尾最后一段命令
        cmd_ls.append(cur_cmd)
        dis_ls.append(np.round(dis-cur_dis, 2))

        # 拼接自然语言提示
        if return_prompt:
            for c, d in zip(cmd_ls, dis_ls):
                instruction += c
                if 'stop' not in c:
                    instruction += (str(np.round(d, 2)) + ' meters. ')
        
        return [cmd_ls, dis_ls], instruction
    
    # 返回：包含从当前时刻起至未来各等间隔时刻，各邻居代理相对于自车坐标系下的轨迹及
    def get_neighbor_agents_future(self, agent_index, iteration=None):
        # current_ego_state = self.scenario.initial_ego_state
        # present_tracked_objects = self.scenario.initial_tracked_objects.tracked_objects
        current_ego_state = self.current_ego_state
        present_tracked_objects = self.scenario.get_tracked_objects_at_iteration(iteration=iteration).tracked_objects

        # Get all future poses of of other agents
        future_tracked_objects = [
            tracked_objects.tracked_objects
            for tracked_objects in self.scenario.get_future_tracked_objects(
                iteration=iteration, time_horizon=self.future_time_horizon, num_samples=self.num_future_poses
            )
        ]

        sampled_future_observations = [present_tracked_objects] + future_tracked_objects
        future_tracked_objects_tensor_list, _ = sampled_tracked_objects_to_tensor_list(sampled_future_observations)
        agent_futures = agent_future_process(current_ego_state, future_tracked_objects_tensor_list, self.num_agents, agent_index)

        return agent_futures
    
    # 分层绘制（底图 --> 主体 --> 轨迹） + 等比例显示
    def plot_scenario(self, data):
        # Create map layers
        # 将车道线、人行横道、路径车道渲染到同一张画布上
        create_map_raster(data['lanes'], data['crosswalks'], data['route_lanes'])

        # Create agent layers
        create_ego_raster(data['ego_agent_past'][-1]) # 自车当前（最后一帧）位置
        create_agents_raster(data['neighbor_agents_past'][:, -1]) # 邻居当前（最后一帧）位置

        # Draw past and future trajectories
        draw_trajectory(data['ego_agent_past'], data['neighbor_agents_past'])
        draw_trajectory(data['ego_agent_future'], data['neighbor_agents_future'])

        # plt.gca(): 获取当前坐标轴（Axes）对象， set_aspect('equal') x 轴和 y 轴缩放比例设为 1：1
        plt.gca().set_aspect('equal')
        plt.tight_layout() # 自动调整子图参数，使各元素不会重叠

    def save_to_disk(self, dir, data):
        np.savez(f"{dir}/np_data/{data['map_name']}_{data['token']}_{data['iter']}.npz", **data)

    # debug :
    def work(self, save_dir, debug=False, start_s=None):
        prompt_data_ls = []
        save_itr = 100
        scenario_ls = self._scenarios[start_s:start_s+save_itr]
        # debug_ls = os.listdir(f"{save_dir}/map_v2")
        # 遍历每个场景 ii 为编号，共 save_iter = 100 个
        for ii, scenario in enumerate(scenario_ls):
            print(f"Processing scenario: {ii}/{len(scenario_ls)}", flush=True) 
            # 遍历该场景中每个时刻（帧）
            for iter in tqdm(range(len(scenario._lidarpc_tokens))):
                # if iter%80!=0:
                #     continue
                map_name = scenario._map_name
                token = scenario.token
                self.scenario = scenario
                self.map_api = scenario.map_api  
                self.current_ego_state = scenario.get_ego_state_at_iteration(iter)      

                # get agent (ego and neighbor) past tracks
                ego_agent_past, time_stamps_past = self.get_ego_agent(iteration=iter)
                neighbor_agents_past, neighbor_agents_types = self.get_neighbor_agents(iteration=iter)
                ego_agent_past, neighbor_agents_past, neighbor_indices = \
                    agent_past_process(ego_agent_past, time_stamps_past, neighbor_agents_past, neighbor_agents_types, self.num_agents)

                # get vector set map
                vector_map = self.get_map(iteration=iter)

                # get agent future tracks
                ego_agent_future = self.get_ego_agent_future(iteration=iter) # 返回未来的相对轨迹点
                instruction, prompt = self.get_instruction(ego_agent_future, return_prompt=True) # 返回 命令-距离列表和指令列表
                neighbor_agents_future = self.get_neighbor_agents_future(neighbor_indices, iteration=iter) # 返回指定索引 neighbor_indices 对应 agent 的未来轨迹
                
                # for ego_v_a_predictor
                # current_ego_state = scenario.get_ego_state_at_iteration(iter)
                # 从 current_ego_state 中分别提取后轴线速度和加速度，再拼接为 current_v_a
                current_ego_state = self.current_ego_state
                cur_v = current_ego_state.dynamic_car_state.rear_axle_velocity_2d 
                current_v = np.array([cur_v.x, cur_v.y])
                cur_a = current_ego_state.dynamic_car_state.rear_axle_acceleration_2d
                current_a = np.array([cur_a.x, cur_a.y])
                current_v_a = np.array([cur_v.x, cur_v.y, cur_a.x, cur_a.y])
                
                # neighbour_lane
                current_lane = find_current_lane(self.map_api, current_ego_state.car_footprint.center)
                if current_lane is None:
                    continue
                neighbour_lane_id = current_lane.adjacent_edges
                # 用 1,0 表示左/右侧各自是否存在相邻车道
                left_lane = np.array([1]) if neighbour_lane_id[0] is not None else np.array([0])
                right_lane = np.array([1]) if neighbour_lane_id[1] is not None else np.array([0])
                neighbour_lane = np.array([left_lane, right_lane])
                
                # acceleration_classification 加减速判断
                # 用当前速度向量 current_v 与 0.5s 后加速度向量内积判断 
                # 正值且明显 --> 加速，负值且明显 --> 减速，否则：保持速度
                ego_future_state = scenario.get_ego_future_trajectory(iter, time_horizon=0.5, num_samples=1)
                future_acc = [s.dynamic_car_state.rear_axle_acceleration_2d.array for s in ego_future_state][0]
                dot_product = current_v[0] * future_acc[0] + current_v[1] * future_acc[1]
                if dot_product>0.1:
                    acc_classification = np.array([1,0,0]) #acc
                elif dot_product<-0.1:
                    acc_classification = np.array([0,1,0]) #dec
                else:
                    acc_classification = np.array([0,0,1]) #keep
                
                # lane_change
                # 在给定 5s, 50 个采样点的未来轨迹，检测是否发生变道，用 [1]/[0] 标记 “变道” 或 “未变道”
                ego_future_state_long_horizon = scenario.get_ego_future_trajectory(iter, time_horizon=5, num_samples=50)
                ego_future_state_long_horizon = [s for s in ego_future_state_long_horizon]
                lane_change_data = find_lane_change(ego_future_state_long_horizon, self.map_api) # list of LaneChangeData, include duration, start lane, final lane...
                if len(lane_change_data)==0:
                    lane_change = np.array([0]) # not change
                else:
                    lane_change = np.array([1]) # change
            
                # traffic light
                # current_lane = find_current_lane(map_api, current_ego_state.car_footprint.center)
                # 返回解释：
                # traffic_light_for_lanes : 针对左右车道的 one-hot 灯色编码
                # ego_lane_flag : 自车当前车道是否有信号灯
                # distance : 自车到信号灯的距离
                traffic_light_ls = scenario.get_traffic_light_status_at_iteration(iter)
                traffic_light_for_lanes, ego_lane_flag, distance = encode_traffic_light(current_lane, traffic_light_ls, current_ego_state.car_footprint.center) # traffic_light_for_lanes is one hot vector

                # gather data
                data = {"map_name": map_name, "token": token, "ego_agent_past": ego_agent_past, "ego_agent_future": ego_agent_future,
                        "neighbor_agents_past": neighbor_agents_past, "neighbor_agents_future": neighbor_agents_future, "instruction": instruction, "iter": iter,
                        "ego_v_a": current_v_a, "neighbour_lane": neighbour_lane, "acc_classification": acc_classification, "lane_change": lane_change, "traffic_light": traffic_light_for_lanes, "ego_lane_flag": ego_lane_flag}
                data.update(vector_map)

                # visualization
                if debug:
                    self.plot_scenario(data)
                # save to disk
                prompt_data = {}
                prompt_data['input'] = f"Role: You are now an autonomous driving driver, and I will provide you with the environment information including Ego Car Information, Agents Information and Map Information.\n\nEnvironment: <map>\n\nNevigation instructions: {prompt}\n\nYou need to fully understand environmental information, discover important information in the environment, and predict future actions.\n\nFinal Answer:\n\n"
                prompt_data['target'] = ''
                # prompt_data['map_info'] = f"{save_dir}/map/{map_name}_{token}_{iter}.npz"
                prompt_data['map_info'] = f"{save_dir}/np_data/{data['map_name']}_{data['token']}_{data['iter']}.npz"
                prompt_data_ls.append(prompt_data)
                if not debug:
                    self.save_to_disk(save_dir, data)
            
        if not debug:
            with open(f"{save_dir}/jsons/{data['map_name']}_part_{start_s}.json", 'w') as f:
                json.dump(prompt_data_ls, f, indent=2)
            

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Data Processing')
    parser.add_argument('--data_path', type=str, help='path to raw data')
    parser.add_argument('--map_path', type=str, help='path to map data')
    parser.add_argument('--save_path', type=str, help='path to save processed data')
    parser.add_argument('--scenarios_per_type', type=int, default=None, help='number of scenarios per type')
    parser.add_argument('--total_scenarios', type=int, default=None, help='limit total number of scenarios')
    # parser.add_argument('--total_scenarios', default=None, help='limit total number of scenarios')
    parser.add_argument('--shuffle_scenarios', type=bool, default=False, help='shuffle scenarios')
    parser.add_argument('--debug', action="store_true", help='if visualize the data output', default=False)
    parser.add_argument('--start_s', type=int, default=None, help='scenario start to process')
    parser.add_argument('--scenario_cache', '-c', type=str, default=None, help='cache')
    args = parser.parse_args()

    # create save folder
    os.makedirs(args.save_path, exist_ok=True)
    os.makedirs(f"{args.save_path}/np_data", exist_ok=True)
    os.makedirs(f"{args.save_path}/jsons", exist_ok=True)
 
    # get scenarios
    map_version = "nuplan-maps-v1.0"    
    sensor_root = None
    db_files = None
    scenarios = None
    if args.scenario_cache:
        try:
            scenarios = pickle.load(open(args.scenario_cache, "rb"))
        except:
            print("Cache file not found")
    if not scenarios:
        scenario_mapping = ScenarioMapping(scenario_map=get_scenario_map(), subsample_ratio_override=0.5)
        builder = NuPlanScenarioBuilder(args.data_path, args.map_path, sensor_root, db_files, map_version, scenario_mapping=scenario_mapping)
        # scenario_filter = ScenarioFilter(*get_filter_parameters(None, args.total_scenarios, args.shuffle_scenarios))
        scenario_filter = ScenarioFilter(*get_filter_parameters(args.scenarios_per_type, args.total_scenarios, args.shuffle_scenarios))
        worker = SingleMachineParallelExecutor(use_process_pool=True)
        scenarios = builder.get_scenarios(scenario_filter, worker)
        if args.scenario_cache:
            pickle.dump(scenarios, open(args.scenario_cache, "wb"))
        del worker, builder, scenario_filter, scenario_mapping
    print(f"Total number of scenarios: {len(scenarios)}")
    # process data

    
    processor = DataProcessor(scenarios)
    processor.work(args.save_path, debug=args.debug, start_s=args.start_s)
