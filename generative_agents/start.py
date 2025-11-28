import os
import copy
import json
import argparse
import datetime

from dotenv import load_dotenv, find_dotenv

from modules.game import create_game, get_game
from modules import utils, memory

personas = [
    # "阿伊莎", "克劳斯", "玛丽亚", "沃尔夫冈",  # 学生
    # "梅", "约翰", "埃迪",  # 家庭：教授、药店主人、学生
    "简", "汤姆",  # 家庭：家庭主妇、市场主人
    # "卡门", "塔玛拉",  # 室友：供应店主人、儿童读物作家
    # "亚瑟", "伊莎贝拉",  # 酒吧老板、咖啡馆老板
    # "山姆", "詹妮弗",  # 家庭：退役军官、水彩画家
    # "弗朗西斯科", "海莉", "拉吉夫", "拉托亚",  # 共居空间：喜剧演员、作家、画家、摄影师
    # "阿比盖尔", "卡洛斯", "乔治", "瑞恩", "山本百合子", "亚当",  # 动画师、诗人、数学家、软件工程师、税务律师、哲学家
]


class SimulateServer:
    def __init__(self, name, static_root, checkpoints_folder, config, start_step=0, verbose="info", log_file="", 
        collab_mode="baseline",
        meeting_time=None,
        meeting_topic="讨论新项目",
        meeting_place="汤姆和简的卧室",):
        self.name = name
        self.static_root = static_root
        self.checkpoints_folder = checkpoints_folder

        # 历史存档数据（用于断点恢复）
        self.config = config

        os.makedirs(checkpoints_folder, exist_ok=True)

        # 载入历史对话数据（用于断点恢复）
        self.conversation_log = f"{checkpoints_folder}/conversation.json"
        if os.path.exists(self.conversation_log):
            with open(self.conversation_log, "r", encoding="utf-8") as f:
                conversation = json.load(f)
        else:
            conversation = {}

        if len(log_file) > 0:
            self.logger = utils.create_file_logger(f"{checkpoints_folder}/{log_file}", verbose)
        else:
            self.logger = utils.create_io_logger(verbose)

        # 创建游戏
        game = create_game(name, static_root, config, conversation, logger=self.logger)
        game.reset_game()

        self.game = get_game()

         # Only inject when starting a fresh simulation, not when resuming.
        if start_step == 0 and meeting_time:
            agents = ["简", "汤姆"]
            self.logger.info(
                f"[INFO] injecting meeting ({collab_mode}) for {agents} at {meeting_time}, "
                f"place='{meeting_place}', topic='{meeting_topic}'"
            )
            if collab_mode == "baseline":
                self._inject_meeting_memory_soft(
                    agents=agents,
                    when=meeting_time,
                    topic=meeting_topic,
                    place_keyword=meeting_place,
                )
            elif collab_mode == "centralized":
                self._inject_meeting_memory_centralized(
                    agents=agents,
                    when=meeting_time,
                    topic=meeting_topic,
                    place_keyword=meeting_place,
                )
        
        self.tile_size = self.game.maze.tile_size
        self.agent_status = {}
        if "agent_base" in config:
            agent_base = config["agent_base"]
        else:
            agent_base = {}
        for agent_name, agent in config["agents"].items():
            agent_config = copy.deepcopy(agent_base)
            agent_config.update(self.load_static(agent["config_path"]))
            self.agent_status[agent_name] = {
                "coord": agent_config["coord"],
                "path": [],
            }
        self.think_interval = max(
            a.think_config["interval"] for a in self.game.agents.values()
        )
        self.start_step = start_step

    def simulate(self, step, stride=0):
        timer = utils.get_timer()
        for i in range(self.start_step, self.start_step + step):
            

            title = "Simulate Step[{}/{}, time: {}]".format(i+1, self.start_step + step, timer.get_date())
            self.logger.info("\n" + utils.split_line(title, "="))
            for name, status in self.agent_status.items():
                # # 如果当前时间处在某个会议时间窗内，把 agent 传送到会议地点 hard injection
                # for m in self.meetings:
                #     if name in m["agents"] and m["start"] <= now_dt < m["end"]:
                #         if m.get("coord") is not None:
                #             status["coord"] = m["coord"]
                #         break

                plan = self.game.agent_think(name, status)["plan"]
                agent = self.game.get_agent(name)
                if name not in self.config["agents"]:
                    self.config["agents"][name] = {}
                self.config["agents"][name].update(agent.to_dict())
                if plan.get("path"):
                    status["coord"], status["path"] = plan["path"][-1], []
                self.config["agents"][name].update(
                    # {"coord": status["coord"], "path": plan["path"]}
                    {"coord": status["coord"]}
                )

            sim_time = timer.get_date("%Y%m%d-%H:%M")
            self.config.update(
                {
                    "time": sim_time,
                    "step": i + 1,
                }
            )
            # 保存Agent活动数据
            with open(f"{self.checkpoints_folder}/simulate-{sim_time.replace(':', '')}.json", "w", encoding="utf-8") as f:
                f.write(json.dumps(self.config, indent=2, ensure_ascii=False))
            # 保存对话数据
            with open(f"{self.checkpoints_folder}/conversation.json", "w", encoding="utf-8") as f:
                f.write(json.dumps(self.game.conversation, indent=2, ensure_ascii=False))

            if stride > 0:
                timer.forward(stride)

    def load_static(self, path):
        return utils.load_dict(os.path.join(self.static_root, path))

    # ----------------------------------------------------------------------
    # BASELINE: soft memory injection with required location + higher poignancy
    # ----------------------------------------------------------------------
    def _inject_meeting_memory_soft(self, agents, when, topic, place_keyword=None):
        """
        Soft control:
        - Add a 'thought' concept to each agent's associative memory.
        - Includes explicit time & location in describe + address.
        - Increases the concept's poignancy to make retrieval more likely.
        """
        # 时间：会议发生的时间（未来）
        meeting_dt = utils.to_date(when, "%Y%m%d-%H:%M")
        # 概念创建时间：现在（仿佛角色此刻在“计划”未来会议）
        now = utils.get_timer().get_date()
        expire = meeting_dt + datetime.timedelta(days=2)

        for name in agents:
            agent = self.game.get_agent(name)

            # Try to find the meeting location from spatial memory
            address = None
            if place_keyword:
                try:
                    address = agent.spatial.find_address(place_keyword, as_list=True)
                except Exception:
                    address = None

            # Fallback: use current tile if we can't find the keyword
            if not address:
                address = agent.get_tile().get_address(as_list=True)

            others = [a for a in agents if a != name]
            others_str = "、".join(others) if others else "自己"

            describe = (
                f"{name} 计划在 {meeting_dt.strftime('%m月%d日 %H:%M')} "
                f"和 {others_str} 在 {address[-1]} 开会，讨论 {topic}。"
            )

            event = memory.Event(
                subject=name,
                predicate="计划",
                object="开会",
                address=address,
                describe=describe,
            )

            # Add as a 'thought' concept
            node = agent._add_concept(
                "thought",
                event,
                create=now,
                expire=expire,
            )

            # Bump concept-level poignancy to increase chance of retrieval
            try:
                if node is not None and hasattr(node, "poignancy"):
                    current_p = getattr(node, "poignancy", 0)
                    node.poignancy = max(current_p, 5)
            except Exception:
                # Be robust to any internal differences in Node implementation
                pass

    # ----------------------------------------------------------------------
    # CENTRALIZED: baseline + "system style" reminder in currently + agent-level poignancy
    # ----------------------------------------------------------------------
    def _inject_meeting_memory_centralized(self, agents, when, topic, place_keyword=None):
        """
        Centralized control:
        - First do the baseline soft injection.
        - Additionally:
          * Append an "important task" sentence into each agent's `currently`,
            so it shows up in almost every planning prompt.
          * Raise agent-level poignancy, making reflection more likely.
        - Still does NOT directly modify daily_schedule or actions.
        """
        # 先做 baseline 版本的软注入
        self._inject_meeting_memory_soft(agents, when, topic, place_keyword)

        meeting_dt = utils.to_date(when, "%Y%m%d-%H:%M")
        date_str = meeting_dt.strftime("%m月%d日")
        time_str = meeting_dt.strftime("%H:%M")

        for name in agents:
            agent = self.game.get_agent(name)
            others = [a for a in agents if a != name]
            others_str = "、".join(others) if others else "其他人"

            # Try to re-resolve the address for better wording
            address = None
            if place_keyword:
                try:
                    address = agent.spatial.find_address(place_keyword, as_list=True)
                except Exception:
                    address = None
            if not address:
                address = agent.get_tile().get_address(as_list=True)
            location_str = address[-1]

            # 强化版“系统提示”：写入 currently 字段
            important_msg = (
                f"\n今天有一件非常重要的事情："
                f"{name} 必须在 {date_str} {time_str} 和 {others_str} "
                f"在 {location_str} 开会，讨论 {topic}。"
                f"请优先安排这个会议，即使需要调整日常计划。"
            )

            try:
                if hasattr(agent, "scratch") and hasattr(agent.scratch, "currently"):
                    base_currently = agent.scratch.currently or ""
                    agent.scratch.currently = base_currently + important_msg
            except Exception:
                pass

            # 提升 agent 的整体 poignancy，让“重要事件”更容易触发反思
            try:
                if hasattr(agent, "status"):
                    if isinstance(agent.status, dict):
                        cur_p = agent.status.get("poignancy", 0)
                        agent.status["poignancy"] = max(cur_p, 8)
                    elif hasattr(agent.status, "poignancy"):
                        cur_p = getattr(agent.status, "poignancy", 0)
                        agent.status.poignancy = max(cur_p, 8)
            except Exception:
                pass

    # def _inject_meeting_memory(self, agents, when, topic):
    #     """
    #     软硬结合控制：
    #     1）在记忆中注入“会议计划”（soft control）
    #     2）记录一个会议对象，在 simulate() 里用坐标做轻微硬控（teleport）
    #     """
    #     meeting_dt = utils.to_date(when, "%Y%m%d-%H:%M")
    #     expire = meeting_dt + datetime.timedelta(days=2)

    #     # 会议持续时间（分钟）：可以改成 10 / 20 / 30
    #     duration_minutes = 20
    #     meeting_end = meeting_dt + datetime.timedelta(minutes=duration_minutes)

    #     # 以第一个人的初始坐标作为会议地点（例如：卧室的床）
    #     if agents:
    #         first_agent_name = agents[0]
    #         meeting_coord = tuple(self.agent_status[first_agent_name]["coord"])
    #     else:
    #         meeting_coord = None

    #     # 记录会议，用于 simulate() 里的硬控
    #     self.meetings.append(
    #         {
    #             "agents": agents,
    #             "start": meeting_dt,
    #             "end": meeting_end,
    #             "coord": meeting_coord,
    #             "topic": topic,
    #         }
    #     )

    #     # 继续原来的“软注入”：在概念记忆里加一条“计划开会”的 thought
    #     for name in agents:
    #         agent = self.game.get_agent(name)

    #         # 使用当前 tile 作为会议地点（通常是卧室的床）
    #         address = agent.get_tile().get_address(as_list=True)

    #         others = [a for a in agents if a != name]
    #         others_str = "、".join(others) if others else "自己"

    #         describe = (
    #             f"{name} 计划在 {meeting_dt.strftime('%m月%d日 %H:%M')} "
    #             f"和 {others_str} 在 {address[-1]} 讨论 {topic}。"
    #         )
    #         event = memory.Event(
    #             subject=name,
    #             predicate="计划",
    #             object="开会",
    #             address=address,
    #             describe=describe,
    #         )

    #         # 加入高层“thought”记忆（软控制）
    #         agent._add_concept("thought", event, create=meeting_dt, expire=expire)

# 从存档数据中载入配置，用于断点恢复
def get_config_from_log(checkpoints_folder):
    files = sorted(os.listdir(checkpoints_folder))

    json_files = list()
    for file_name in files:
        if file_name.endswith(".json") and file_name != "conversation.json":
            json_files.append(os.path.join(checkpoints_folder, file_name))

    if len(json_files) < 1:
        return None

    with open(json_files[-1], "r", encoding="utf-8") as f:
        config = json.load(f)

    assets_root = os.path.join("assets", "village")

    start_time = datetime.datetime.strptime(config["time"], "%Y%m%d-%H:%M")
    start_time += datetime.timedelta(minutes=config["stride"])
    config["time"] = {"start": start_time.strftime("%Y%m%d-%H:%M")}
    agents = config["agents"]
    for a in agents:
        config["agents"][a]["config_path"] = os.path.join(assets_root, "agents", a.replace(" ", "_"), "agent.json")

    return config


# 为新游戏创建配置
def get_config(start_time="20240213-09:30", stride=15, agents=None):
    with open("data/config.json", "r", encoding="utf-8") as f:
        json_data = json.load(f)
        agent_config = json_data["agent"]

    assets_root = os.path.join("assets", "village")
    config = {
        "stride": stride,
        "time": {"start": start_time},
        "maze": {"path": os.path.join(assets_root, "maze.json")},
        "agent_base": agent_config,
        "agents": {},
    }
    for a in agents:
        config["agents"][a] = {
            "config_path": os.path.join(
                assets_root, "agents", a.replace(" ", "_"), "agent.json"
            ),
        }
    return config


load_dotenv(find_dotenv())

parser = argparse.ArgumentParser(description="console for village")
parser.add_argument("--name", type=str, default="", help="The simulation name")
parser.add_argument("--start", type=str, default="20240213-09:30", help="The starting time of the simulated ville")
parser.add_argument("--resume", action="store_true", help="Resume running the simulation")
parser.add_argument("--step", type=int, default=10, help="The simulate step")
parser.add_argument("--stride", type=int, default=10, help="The step stride in minute")
parser.add_argument("--verbose", type=str, default="debug", help="The verbose level")
parser.add_argument("--log", type=str, default="", help="Name of the log file")

parser.add_argument(
    "--collab_mode",
    type=str,
    default="baseline",
    choices=["baseline", "centralized"],
    help="Collaboration mechanism: 'baseline' (soft memory) or 'centralized' (soft + system reminder).",
)
parser.add_argument(
    "--meeting_time",
    type=str,
    default=None,
    help="Meeting time in format YYYYMMDD-HH:MM, e.g. 20250213-09:30. If omitted, no meeting is injected.",
)
parser.add_argument(
    "--meeting_topic",
    type=str,
    default="讨论新项目",
    help="Meeting topic text used in injected memory.",
)
parser.add_argument(
    "--meeting_place",
    type=str,
    default="汤姆和简的卧室",
    help="Keyword for meeting place (used with agent.spatial.find_address).",
)
args = parser.parse_args()

if __name__ == "__main__":
    checkpoints_path = "results/checkpoints"

    name = args.name
    if len(name) < 1:
        name = input("Please enter a simulation name (e.g. sim-test): ")

    resume = args.resume
    if resume:
        while not os.path.exists(f"{checkpoints_path}/{name}"):
            name = input(f"'{name}' doesn't exists, please re-enter the simulation name: ")
    else:
        while os.path.exists(f"{checkpoints_path}/{name}"):
            name = input(f"The name '{name}' already exists, please enter a new name: ")

    checkpoints_folder = f"{checkpoints_path}/{name}"

    start_time = args.start
    if resume:
        sim_config = get_config_from_log(checkpoints_folder)
        if sim_config is None:
            print("No checkpoint file found to resume running.")
            exit(0)
        start_step = sim_config["step"]
    else:
        sim_config = get_config(start_time, args.stride, personas)
        start_step = 0

    static_root = "frontend/static"

    server = SimulateServer(name, static_root, checkpoints_folder, sim_config, start_step, args.verbose, args.log, collab_mode=args.collab_mode,
        meeting_time=args.meeting_time,
        meeting_topic=args.meeting_topic,
        meeting_place=args.meeting_place)
    server.simulate(args.step, args.stride)
