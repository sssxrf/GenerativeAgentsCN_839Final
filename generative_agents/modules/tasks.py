# modules/tasks.py
import datetime

from . import memory, utils

class MeetingTask:
    """
    会议任务：负责按 baseline / centralized 两种模式，
    给 agents 注入“会议计划”相关的记忆和提示。

    用法：
        meeting_task = MeetingTask(game, config, logger)
        meeting_task.setup(
            mode="baseline" 或 "centralized",
            agents=["简", "汤姆"],
            when="20250213-11:00",
            topic="讨论新项目",
            place_keyword="汤姆和简的卧室",
        )
    """

    def __init__(self, game, config, logger=None):
        self.game = game
        self.config = config
        self.logger = logger

    def _log(self, msg, level="info"):
        if not self.logger:
            return
        fn = getattr(self.logger, level, None)
        if fn:
            fn(msg)

    def setup(self, mode, agents, when, topic, place_keyword=None):
        """
        mode = baseline     -> 只做软注入 (thought 记忆)
        mode = centralized  -> baseline + currently 提示 + 提高 agent-level poignancy
        """
        self._log(
            f"[MEETING] setup meeting task mode={mode}, agents={agents}, "
            f"when={when}, place={place_keyword}, topic={topic}"
        )
        if mode == "centralized":
            self._inject_centralized(agents, when, topic, place_keyword)
        else:
            self._inject_baseline(agents, when, topic, place_keyword)

    # ------------------------------------------------------------------
    # baseline: 原来的 _inject_meeting_memory_soft
    # ------------------------------------------------------------------
    def _inject_baseline(self, agents, when, topic, place_keyword=None):
        """
        Soft control:
        - 给每个 agent 加入一个 'thought' 概念记忆：
          * 带有明确的时间、地点、会议主题
        - 略微提高该概念的 poignancy（让它更容易被想起）
        """
        meeting_dt = utils.to_date(when, "%Y%m%d-%H:%M")
        now = utils.get_timer().get_date()
        expire = meeting_dt + datetime.timedelta(days=2)

        for name in agents:
            agent = self.game.get_agent(name)

            # 找会议地点
            address = None
            if place_keyword:
                try:
                    address = agent.spatial.find_address(place_keyword, as_list=True)
                except Exception:
                    address = None

            # 找不到就用当前 tile 作为地点
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

            # 加入 thought 概念
            try:
                node = agent._add_concept(
                    "thought",
                    event,
                    create=now,
                    expire=expire,
                )
            except TypeError:
                # 兼容 create/expire 不在签名中的情况
                node = agent._add_concept("thought", event)

            # 提高该记忆节点的 poignancy
            try:
                if node is not None and hasattr(node, "poignancy"):
                    current_p = getattr(node, "poignancy", 0)
                    node.poignancy = max(current_p, 7)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # centralized: baseline + currently + agent-level poignancy
    # ------------------------------------------------------------------
    def _inject_centralized(self, agents, when, topic, place_keyword=None):
        """
        Centralized 模式：
        1）先做 baseline 的软注入
        2）再往每个 agent.scratch.currently 追加一段“系统提示”
        3）提高 agent.status.poignancy，增加反思概率
        """
        # 先做 baseline
        self._inject_baseline(agents, when, topic, place_keyword)

        meeting_dt = utils.to_date(when, "%Y%m%d-%H:%M")
        date_str = meeting_dt.strftime("%m月%d日")
        time_str = meeting_dt.strftime("%H:%M")

        for name in agents:
            agent = self.game.get_agent(name)
            others = [a for a in agents if a != name]
            others_str = "、".join(others) if others else "其他人"

            # 再解析一下地点，给 currently 用一份好读的字符串
            address = None
            if place_keyword:
                try:
                    address = agent.spatial.find_address(place_keyword, as_list=True)
                except Exception:
                    address = None
            if not address:
                address = agent.get_tile().get_address(as_list=True)
            location_str = address[-1]

            important_msg = (
                f"\n今天有一件非常重要的事情："
                f"{name} 必须在 {date_str} {time_str} 和 {others_str} "
                f"在 {location_str} 开会，讨论 {topic}。"
                f"请优先安排这个会议，即使需要调整日常计划。"
            )

            # 写入 currently
            try:
                if hasattr(agent, "scratch") and hasattr(agent.scratch, "currently"):
                    base_currently = agent.scratch.currently or ""
                    agent.scratch.currently = base_currently + important_msg
            except Exception:
                pass

            # 提升 agent-level poignancy
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



class EddieRescueTask:
    """
    特殊协作任务：“救埃迪”。
    负责把任务相关的记忆注入到给定的 agents 里。

    使用方式：
        task = EddieRescueTask(game, config, logger)
        task.setup(mode="baseline" or "centralized", agents=["简", "汤姆"])
    """

    def __init__(self, game, config, logger=None):
        self.game = game
        self.config = config
        self.logger = logger

    # ----------- 小工具：日志 -----------
    def _log(self, msg, level="info"):
        if self.logger is None:
            return
        fn = getattr(self.logger, level, None)
        if fn is not None:
            fn(msg)

    # ----------- 外部入口：根据模式注入 -----------
    def setup(self, mode, agents):
        """
        mode == 'baseline'：
            每个 agent 都得到“完整任务 + 子任务清单”。
        mode == 'centralized'：
            只有中心知道整个任务；每个 agent 只收到自己负责的部分。
        """
        self._log(f"[TASK] setup Eddie-rescue task, mode={mode}, agents={agents}")
        if mode == "centralized":
            self._centralized(agents)
        else:
            self._baseline(agents)

    # ----------- 通用工具：往 agent 记忆里塞 thought，并提高 poignancy -----------
    def _add_task_thought(self, agent, event, base_poignancy=6):
        """
        往 agent 的概念记忆中加一个 'thought'，并设置一个偏高的情绪权重。
        """
        # 从 config 里拿仿真开始时间
        time_cfg = self.config.get("time", None)
        if isinstance(time_cfg, dict):
            start_str = time_cfg.get("start")
        else:
            start_str = time_cfg

        create_dt = None
        expire_dt = None
        try:
            if start_str:
                create_dt = utils.to_date(start_str, "%Y%m%d-%H:%M")
                expire_dt = create_dt + datetime.timedelta(days=1)
        except Exception:
            pass

        kwargs = {}
        if create_dt is not None:
            kwargs["create"] = create_dt
        if expire_dt is not None:
            kwargs["expire"] = expire_dt

        # 调用内置的 _add_concept
        try:
            node = agent._add_concept("thought", event, **kwargs)
        except TypeError:
            # 万一签名不支持 create/expire，就只传核心参数
            node = agent._add_concept("thought", event)

        # bump poignancy
        try:
            if node is not None and hasattr(node, "poignancy"):
                cur = getattr(node, "poignancy", 0)
                node.poignancy = max(cur, base_poignancy)
        except Exception:
            pass

        return node

    # ----------- BASELINE：两个 agent 都知道完整任务 + 所有前置条件 -----------
    def _baseline(self, agents):
        """
        baseline 机制：
        - 每个 agent 都得到完整描述：
          * 最终目标：在 林氏家族的房子，埃迪的卧室，床 边准备好
            感冒药、热汤、毯子、竖琴；
          * 要完成的子任务及地点、前置条件。
        """

        # 关键地点（来自 maze.json）
        child_room_addr = ["林氏家族的房子", "埃迪的卧室", "床"]
        card_addr = ["林氏家族的房子", "公共休息室", "公共休息室桌子"]
        pharmacy_shelf_addr = ["柳树市场和药店", "商店", "药店货架"]
        soup_kitchen_addr = ["塔玛拉和卡门的家", "厨房", "烹饪区"]
        blanket_addr = ["亚当的家", "主人房", "床"]
        harp_addr = ["莫雷诺家族的房子", "公共休息室", "竖琴"]

        child_room_str = "，".join(child_room_addr)
        card_str = "，".join(card_addr)
        pharmacy_str = "，".join(pharmacy_shelf_addr)
        soup_kitchen_str = "，".join(soup_kitchen_addr)
        blanket_str = "，".join(blanket_addr)
        harp_str = "，".join(harp_addr)

        for name in agents:
            agent = self.game.get_agent(name)

            describe = (
                f"{name} 得知：林氏家族的孩子埃迪突然发高烧，家里人都很慌张。\n"
                f"最终目标：在 {child_room_str} 让埃迪的床边准备好感冒药、热汤、一条暖和的毯子和一件乐器（竖琴），让他安心休息。\n\n"
                f"要完成这个目标，需要按顺序完成以下子任务，并注意前置条件：\n"
                f"1）先去 {card_str} 找到医保卡，之后买药才可以报销。\n"
                f"2）带着医保卡，去 {pharmacy_str} 购买适合儿童的感冒药，并顺便买煮一碗热汤需要的食材。\n"
                f"3）拿着食材，去 {soup_kitchen_str} 煮一锅热汤。\n"
                f"4）去 {blanket_str} 取一条备用的暖和毯子。\n"
                f"5）去 {harp_str} 把竖琴带走，准备在孩子床边小声演奏安抚他。\n"
                f"6）最后，把感冒药、热汤、毯子和竖琴一起带到 {child_room_str}，陪埃迪说几句话，让他安心休息。\n"
            )

            event = memory.Event(
                subject=name,
                predicate="承担任务",
                object="照顾生病的埃迪",
                address=child_room_addr,
                describe=describe,
            )

            self._add_task_thought(agent, event, base_poignancy=8)

    # ----------- CENTRALIZED：中心只把各自负责部分分配给 agent -----------
    def _centralized(self, agents):
        """
        centralized 机制：
        - 中心知道完整任务，但每个 agent 只收到自己负责的那一部分。
        - 这里约定：
          * 简：医保卡 + 买药 + 食材 + 煮汤 + 把药和汤送到埃迪房间；
          * 汤姆：毯子 + 竖琴 + 把它们送到埃迪房间。
        """

        # 关键地点（和 baseline 一致）
        child_room_addr = ["林氏家族的房子", "埃迪的卧室", "床"]
        card_addr = ["林氏家族的房子", "公共休息室", "公共休息室桌子"]
        pharmacy_shelf_addr = ["柳树市场和药店", "商店", "药店货架"]
        soup_kitchen_addr = ["塔玛拉和卡门的家", "厨房", "烹饪区"]
        blanket_addr = ["亚当的家", "主人房", "床"]
        harp_addr = ["莫雷诺家族的房子", "公共休息室", "竖琴"]

        child_room_str = "，".join(child_room_addr)
        card_str = "，".join(card_addr)
        pharmacy_str = "，".join(pharmacy_shelf_addr)
        soup_kitchen_str = "，".join(soup_kitchen_addr)
        blanket_str = "，".join(blanket_addr)
        harp_str = "，".join(harp_addr)

        # 简负责“药 + 汤”，另一位负责“毯子 + 竖琴”
        cook_agent_name = "简" if "简" in agents else agents[0]
        other_agents = [a for a in agents if a != cook_agent_name]
        blanket_agent_name = other_agents[0] if other_agents else cook_agent_name

        # ---- 1）给负责“药 + 汤”的 agent 下发任务 ----
        if cook_agent_name in self.game.agents:
            agent = self.game.get_agent(cook_agent_name)
            desc_cook = (
                f"镇上的中央协调者告诉 {cook_agent_name}：林氏家族的孩子埃迪生病了，"
                f"你负责照顾任务中的一部分。\n"
                f"你的具体任务是：\n"
                f"1）先去 {card_str} 找到医保卡。\n"
                f"2）带着医保卡，去 {pharmacy_str} 购买合适的感冒药，并买一份可以煮成热汤的食材。\n"
                f"3）带着食材，去 {soup_kitchen_str} 煮一锅热汤。\n"
                f"4）把煮好的热汤和感冒药送到 {child_room_str}，放在埃迪的床边，并安慰他一下。\n\n"
                f"其它物品（毯子、竖琴）会交给其他人负责，你不需要操心。"
            )

            event_cook = memory.Event(
                subject=cook_agent_name,
                predicate="承担任务",
                object="为埃迪准备药物和热汤",
                address=child_room_addr,
                describe=desc_cook,
            )
            self._add_task_thought(agent, event_cook, base_poignancy=7)

            # 在 currently 里加一句“系统提示”
            try:
                if hasattr(agent, "scratch"):
                    base_cur = getattr(agent.scratch, "currently", "") or ""
                    agent.scratch.currently = (
                        base_cur
                        + "\n今天镇上发生了一件紧急的事情："
                          "林氏家族的孩子埃迪生病了，中央只交给你负责医保卡、买药和煮汤的部分任务。"
                          "请在不完全打乱日常生活的前提下优先完成。"
                    )
            except Exception:
                pass

        # ---- 2）给负责“毯子 + 竖琴”的 agent 下发任务 ----
        if blanket_agent_name in self.game.agents:
            agent = self.game.get_agent(blanket_agent_name)
            desc_blanket = (
                f"镇上的中央协调者告诉 {blanket_agent_name}：林氏家族的孩子埃迪生病了，"
                f"你负责照顾任务中的另一部分。\n\n"
                f"你的具体任务是：\n"
                f"1）去 {blanket_str} 取一条暖和的备用毯子。\n"
                f"2）去 {harp_str} 把竖琴带走，准备放在孩子床边演奏安抚他。\n"
                f"3）把毯子和竖琴一起带到 {child_room_str}，放在床边，并陪他待一会。\n\n"
                f"药物和热汤将由其他人准备，你只需要专注完成自己的部分。"
            )

            event_blanket = memory.Event(
                subject=blanket_agent_name,
                predicate="承担任务",
                object="为埃迪准备毯子和竖琴",
                address=child_room_addr,
                describe=desc_blanket,
            )
            self._add_task_thought(agent, event_blanket, base_poignancy=8)

            try:
                if hasattr(agent, "scratch"):
                    base_cur = getattr(agent.scratch, "currently", "") or ""
                    agent.scratch.currently = (
                        base_cur
                        + "\n今天镇上发生了一件紧急的事情："
                          "林氏家族的孩子埃迪生病了，中央只交给你准备毯子和竖琴的部分任务。"
                          "请优先完成准备坛子和竖琴部分任务，日常任务可以暂时取消。"
                    )
            except Exception:
                pass
