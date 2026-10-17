#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股涨停板提示器  zt_popup.py
============================================================
盘中实时监控方案（9:30 - 14:57，仅盘中新增首板提示）：

  盘中新增（9:30 - 14:57）
    每 5 秒轮询涨停池，仅对「盘中首次触板」的新增涨停股弹窗提示，
    弹窗固定在屏幕右下角，极简卡片，3 秒后自动关闭。
    去重规则：
      - S1 竞价集合：启动时先采集一次竞价涨停股记入（不弹 9:26 全景窗），
        盘中候选剔除 S1 —— 竞价已涨停的股票盘中即使回封也不提示；
      - notified 集合：盘中首次触板即提示并记入，此后反复炸板回封均不再提示。

注：9:26 竞价全景弹窗已停用（用户要求维持删除）；S1 采集保留用于盘中去重。

数据源：东方财富涨停板池接口 getTopicZTPool（免费、无需 token、盘中准实时）
  http://push2ex.eastmoney.com/getTopicZTPool
  实测字段：c(代码) n(名称) lbc(连板数) fund(封单资金,元) fbt(首次封板时间,HHMMSS)
            lbt(最后封板时间) zbc(炸板次数) hybk(行业板块→题材归类) zttj(涨停统计)
  实测行为：非交易日传当天日期，自动回退返回最近交易日数据（qdate 字段标注）。

依赖：仅 Python 标准库（urllib + tkinter），无需 pip 安装任何包。
  tkinter 缺失时自动降级为控制台输出模式。

用法（双击 exe 即运行，不开机自启）：
  ZTPopup.exe                         # 正常监控：手动启动，盘中新增涨停提示
  ZTPopup.exe --demo                  # 演示模式：立即用最近交易日真实数据跑一遍流程
  ZTPopup.exe --selftest              # 自检：真实数据 + 去重逻辑断言 + GUI 冒烟测试
  ZTPopup.exe --check-trade-day       # 诊断：只打印今天是否 A股交易日
  ZTPopup.exe --no-gui                # 强制控制台模式（无弹窗，仅日志）
  ZTPopup.exe --interval 3            # 自定义盘中轮询间隔（秒）
  ZTPopup.exe --log-file zt.log       # 自定义日志文件（默认 exe 同目录 zt_popup.log）

运行方式：手动双击启动，不做开机自启、不常驻后台。
  9:26 采集竞价集合 → 9:30 起每 5 秒轮询 → 盘中首次触板弹窗 → 14:57 自行结束；
  关闭窗口/进程即停止。重复双击不会开出第二个实例。
============================================================
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# 配置区
# ---------------------------------------------------------------------------
CONFIG = {
    "auction_at": "09:26:00",      # S1 竞价集合采集时刻（不弹窗）
    "auction_repeat": 2,           # S1 采集重复次数（网络兜底，合并去重）
    "auction_repeat_gap": 20,      # S1 重复采集间隔（秒）
    "show_auction_panel": False,   # 9:26 竞价全景弹窗：已停用（用户要求维持删除）
    "intraday_start": "09:30:00",  # 盘中监控开始时刻
    "intraday_end": "14:57:00",    # 盘中监控结束时刻（尾盘集合竞价前）
    "poll_interval": 5,            # 盘中轮询间隔（秒）
    "max_retry": 3,                # 单次 HTTP 请求重试次数
    "retry_gap": 2,                # 重试间隔（秒，兼容旧配置；实际按 retry_backoff 退避）
    "retry_backoff": (2, 5, 8),    # 逐次退避（秒）：被东财风控 403 时密集重试更容易被判死
    "auto_close_ms": 3000,         # 盘中单股弹窗自动关闭时间（毫秒）
    "queue_limit": 12,             # 盘中弹窗排队上限，超过部分逐只排队（日志完整）
    "pop_gap_ms": 3000,            # 排队弹窗之间的间隔（毫秒）
    "auction_width": 470,          # 竞价全景弹窗宽度
    "auction_height": 580,         # 竞价全景弹窗高度
    "toast_width": 260,            # 盘中单股弹窗宽度（极简卡片）
    "api_url": "http://push2ex.eastmoney.com/getTopicZTPool",
    "api_ut": "7eea3edcaed734bea9cbfc24409ed989",
    "api_dpt": "wz.ztzt",
    "api_pagesize": 600,
    "api_timeout": 8,              # HTTP 超时（秒）
    "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
}

# 深色主题（贴合深蓝/黑灰审美）
THEME = {
    "bg": "#0d1117",          # 窗口底色
    "panel": "#161b22",       # 面板底色
    "panel2": "#1c2330",      # 次级面板
    "border": "#30363d",      # 边框
    "muted": "#8b949e",       # 次要文字
    "text": "#e6edf3",        # 主文字
    "red": "#ff4d4f",         # 涨停红
    "gold": "#ffb648",        # 连板金
    "cyan": "#58a6ff",        # 封单青
    "green": "#3fb950",       # 时间绿
    "close_hover": "#f85149", # 关闭按钮悬停
}


# ---------------------------------------------------------------------------
# 交易日历（A股：周一至周五开市，法定节假日休市）
# ---------------------------------------------------------------------------
# 节假日表仅收录「落在工作日」的休市日（周末由 weekday() 自动排除）。
# 数据来源：上交所《关于2026年部分节假日休市安排的通知》（上证公告〔2025〕45号，
# 2025-12-22 发布）。每年底交易所发布次年安排后，往下面新增一个年份键即可。
TRADE_HOLIDAYS = {
    "2026": {
        "01-01", "01-02",                                          # 元旦 1/1-1/3
        "02-16", "02-17", "02-18", "02-19", "02-20", "02-23",      # 春节 2/15-2/23
        "04-06",                                                   # 清明 4/4-4/6
        "05-01", "05-04", "05-05",                                 # 劳动节 5/1-5/5
        "06-19",                                                   # 端午 6/19-6/21
        "09-25",                                                   # 中秋 9/25-9/27
        "10-01", "10-02", "10-05", "10-06", "10-07",               # 国庆 10/1-10/7
    },
}


def is_trade_day(d=None):
    """是否 A股交易日：周一至周五 且 不在节假日表。d 缺省为今天。"""
    d = d or datetime.date.today()
    if d.weekday() >= 5:                     # 周六(5)/周日(6)
        return False
    return d.strftime("%m-%d") not in TRADE_HOLIDAYS.get(d.strftime("%Y"), set())


def trade_day_reason(d=None):
    """返回日期定性：交易日 / 周末 / 节假日休市。"""
    d = d or datetime.date.today()
    if d.weekday() >= 5:
        return "周末休市"
    if d.strftime("%m-%d") in TRADE_HOLIDAYS.get(d.strftime("%Y"), set()):
        return "节假日休市"
    return "交易日"


# ---------------------------------------------------------------------------
# 数据模型与格式化
# ---------------------------------------------------------------------------
class Stock:
    """单只涨停股记录（从东财接口原始 dict 安全解析）。"""

    __slots__ = ("code", "name", "lbc", "fund", "hybk", "fbt", "lbt", "zbc", "zdp")

    def __init__(self, raw: dict):
        self.code = str(raw.get("c", "")).zfill(6)
        self.name = str(raw.get("n", ""))
        self.lbc = int(raw.get("lbc", 0) or 0)              # 连板数，1=首板
        self.fund = float(raw.get("fund", 0) or 0)          # 封单资金（元）
        self.hybk = str(raw.get("hybk", "") or "未知")       # 行业板块 → 题材归类
        self.fbt = str(raw.get("fbt", "") or "").zfill(6)   # 首次封板时间 HHMMSS
        self.lbt = str(raw.get("lbt", "") or "").zfill(6)   # 最后封板时间 HHMMSS
        self.zbc = int(raw.get("zbc", 0) or 0)              # 炸板次数
        self.zdp = float(raw.get("zdp", 0) or 0)            # 涨停幅度 %

    @property
    def lbc_text(self):
        return "首板" if self.lbc <= 1 else f"{self.lbc}连板"

    @property
    def fund_text(self):
        return fmt_amount(self.fund)

    @property
    def fbt_text(self):
        return fmt_time(self.fbt)

    @property
    def is_auction(self):
        """是否竞价时段（9:30 前）已封板。"""
        return bool(self.fbt) and self.fbt < "093000"

    def line(self):
        return (f"{self.code} {self.name} [{self.lbc_text}] "
                f"封单{self.fund_text} {self.hybk} 首封{self.fbt_text}")

    def __repr__(self):
        return f"<Stock {self.code} {self.name} {self.lbc_text}>"


def fmt_amount(fund: float) -> str:
    """封单金额格式化为 万/亿。"""
    if fund >= 1e8:
        return f"{fund / 1e8:.2f}亿"
    if fund >= 1e4:
        return f"{fund / 1e4:.0f}万"
    return f"{fund:.0f}元"


def fmt_time(hhmmss: str) -> str:
    """'092500' -> '09:25:00'；空值返回 '--'。"""
    if not hhmmss or hhmmss == "000000":
        return "--"
    return f"{hhmmss[0:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}"


# ---------------------------------------------------------------------------
# 数据采集层
# ---------------------------------------------------------------------------
class ZTDataFetcher:
    """东方财富涨停板池采集器：请求 + 重试 + 缓存降级。"""

    def __init__(self):
        self._cache = None        # 最近一次成功结果
        self._cache_qdate = None

    def _request_pool(self, trade_date: str):
        """单次请求，返回原始 pool 列表；失败抛异常。"""
        params = {
            "ut": CONFIG["api_ut"],
            "dpt": CONFIG["api_dpt"],
            "Pageindex": 0,
            "pagesize": CONFIG["api_pagesize"],
            "sort": "fbt:asc",
            "date": trade_date,
        }
        url = CONFIG["api_url"] + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": CONFIG["user_agent"],
            "Referer": "http://quote.eastmoney.com/",
            "Accept": "application/json, text/plain, */*",
            "Connection": "close",
        })
        with urllib.request.urlopen(req, timeout=CONFIG["api_timeout"]) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        if payload.get("rc") != 0 or payload.get("data") is None:
            raise RuntimeError(f"接口返回异常 rc={payload.get('rc')}")
        data = payload["data"]
        qdate = str(data.get("qdate", trade_date))
        return qdate, [Stock(x) for x in (data.get("pool") or [])]

    def fetch_pool(self, trade_date: str):
        """
        带重试与缓存的采集入口。
        返回 (qdate, [Stock, ...])。全部重试失败时返回缓存（若存在）。
        """
        last_err = None
        backoff = CONFIG["retry_backoff"]
        for i in range(CONFIG["max_retry"]):
            try:
                qdate, stocks = self._request_pool(trade_date)
                self._cache, self._cache_qdate = stocks, qdate
                return qdate, stocks
            except Exception as e:  # noqa: BLE001 - 网络兜底场景
                last_err = e
                if i < CONFIG["max_retry"] - 1:
                    delay = backoff[min(i, len(backoff) - 1)]
                    log(f"[采集] 第 {i + 1} 次请求失败（{e}），{delay} 秒后重试")
                    time.sleep(delay)
        if self._cache is not None:
            log(f"[采集] 重试 {CONFIG['max_retry']} 次失败，降级使用缓存({self._cache_qdate})：{last_err}")
            return self._cache_qdate, self._cache
        raise RuntimeError(f"涨停池接口全部失败，且无缓存可用：{last_err}")


# ---------------------------------------------------------------------------
# 去重状态机
# ---------------------------------------------------------------------------
class DedupManager:
    """
    去重判定规则（核心）：
      1. S1 竞价集合：启动时采集竞价涨停股（fbt < 093000），盘中候选池一律剔除
         S1 —— 竞价已涨停的股票盘中即使回封也绝不提示，避免重复打扰。
      2. notified 集合：盘中「首次触板」即提示并记入。
         此后该股无论炸板、回封、再炸板再回封，都不再提示 —— 单次提示保证。
      3. 轮询抖动免疫：单轮缺席导致的短暂消失/重现，因 notified 已存在而天然免疫。
      4. 每日收盘 reset() 清空全部状态。
    """

    def __init__(self):
        self.s1 = {}        # code -> Stock（竞价集合）
        self.notified = {}  # code -> Stock（盘中已提示）

    def set_auction_pool(self, stocks):
        """写入竞价集合，仅收录竞价时段（9:30 前）已封板的股票。"""
        for s in stocks:
            if s.is_auction:
                self.s1[s.code] = s

    def merge_auction_pool(self, stocks):
        """S1 重复采集的合并兜底：增量并入，不覆盖已有记录。"""
        for s in stocks:
            if s.is_auction and s.code not in self.s1:
                self.s1[s.code] = s

    def new_intraday(self, pool_now):
        """
        盘中轮询：返回「真正首次盘中触板」的新增股票。
        判定：在池中 && 不在 S1 && 不在 notified。
        """
        return [s for s in pool_now
                if s.code not in self.s1 and s.code not in self.notified]

    def mark_notified(self, stocks):
        for s in stocks:
            self.notified[s.code] = s

    def reset(self):
        self.s1.clear()
        self.notified.clear()


# ---------------------------------------------------------------------------
# 弹窗 UI 层（tkinter 深色主题，右下角定位）
# ---------------------------------------------------------------------------
class ZTNotifier:
    """弹窗管理器：盘中单股窗（3 秒自动关，排队限流）。9:26 全景窗已停用。"""

    def __init__(self, root):
        self.root = root
        self.queue = []          # 待弹的盘中单股
        self.current = None      # 当前显示中的单股窗
        self._popping = False

    # ---- 屏幕右下角定位 ----
    @staticmethod
    def _place_bottom_right(win, w, h, margin=16, taskbar_offset=70):
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = sw - w - margin
        y = sh - h - taskbar_offset  # 任务栏上方留白
        win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    # ---------- 盘中单股弹窗（3 秒自动关，排队限流） ----------
    def enqueue_intraday(self, stock, is_demo=False):
        self.queue.append((stock, is_demo))
        if not self._popping:
            self._schedule_pop()

    def _schedule_pop(self):
        self._popping = True
        self.root.after(CONFIG["pop_gap_ms"], self._pop_next)

    def _pop_next(self):
        if self.current is not None and self.current.winfo_exists():
            self.current.destroy()
            self.current = None
        if not self.queue:
            self._popping = False
            return
        stock, is_demo = self.queue.pop(0)
        self._show_toast(stock, is_demo)
        self.root.after(CONFIG["auto_close_ms"], self._close_toast)
        self._schedule_pop()  # 继续排下一只

    def _close_toast(self):
        if self.current is not None and self.current.winfo_exists():
            self.current.destroy()
        self.current = None

    def _show_toast(self, stock, is_demo):
        """极简盘中弹窗：仅 名称/连板数/封单额/涨停原因 四项，无边框装饰，3 秒自动关。"""
        import tkinter as tk
        t = THEME
        w = CONFIG["toast_width"]
        win = self.current = tk.Toplevel(self.root)
        win.title("盘中新板")
        win.configure(bg=t["border"])          # 外框色 = 1px 细边框
        win.attributes("-topmost", True)
        win.overrideredirect(True)

        body = tk.Frame(win, bg=t["bg"], padx=10, pady=7)
        body.pack(fill="both", expand=True)

        # 第 1 行：名称 + 连板数（名称大字，连板数涨停红）
        row1 = tk.Frame(body, bg=t["bg"])
        row1.pack(fill="x")
        tk.Label(row1, text=stock.name, bg=t["bg"], fg=t["text"],
                 font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        tk.Label(row1, text=f"  [{stock.lbc_text}]", bg=t["bg"], fg=t["red"],
                 font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        if is_demo:
            tk.Label(row1, text=" 演示", bg=t["bg"], fg=t["muted"],
                     font=("Microsoft YaHei UI", 9)).pack(side="left", pady=(8, 0))

        # 第 2 行：封单额
        tk.Label(body, text=f"封单 {stock.fund_text}", bg=t["bg"], fg=t["cyan"],
                 font=("Microsoft YaHei UI", 11)).pack(anchor="w", pady=(5, 0))
        # 第 3 行：涨停原因
        tk.Label(body, text=f"原因 {stock.hybk}", bg=t["bg"], fg=t["gold"],
                 font=("Microsoft YaHei UI", 11)).pack(anchor="w", pady=(2, 0))

        win.update_idletasks()
        self._place_bottom_right(win, w, body.winfo_reqheight() + 2)
        win.lift()
        log(f"[提示] 盘中首板 {stock.code} {stock.name} [{stock.lbc_text}] "
            f"封单{stock.fund_text} 题材:{stock.hybk}")


# ---------------------------------------------------------------------------
# 状态窗（任务栏可见）：让用户随时看到"程序在跑 / 在哪个阶段 / 今天有没有数据"
# ---------------------------------------------------------------------------
class StatusPanel:
    """常驻状态窗。关掉它等于退出程序。"""

    ROWS = (("phase", "当前状态"), ("qdate", "数据日"), ("pool", "涨停池"),
            ("notified", "今日已提示"), ("scan", "上次扫描"))

    def __init__(self, root, app):
        import tkinter as tk
        self.tk = tk
        self.root = root
        self.app = app
        self._hint_text = None
        t = THEME
        f9 = ("Microsoft YaHei UI", 9)

        root.title("涨停板提示器")
        root.configure(bg=t["bg"])
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        ico = asset_path("ztpopup.ico")
        if ico:
            try:
                root.iconbitmap(default=ico)
            except Exception:  # noqa: BLE001 - 图标缺失不影响运行
                pass

        head = tk.Frame(root, bg=t["bg"], padx=12, pady=10)
        head.pack(fill="x")
        tk.Label(head, text="涨停板提示器", bg=t["bg"], fg=t["text"],
                 font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        self.dot = tk.Label(head, text="● 运行中", bg=t["bg"], fg=t["green"], font=f9)
        self.dot.pack(side="right")

        tk.Frame(root, bg=t["border"], height=1).pack(fill="x")

        body = tk.Frame(root, bg=t["bg"], padx=12, pady=8)
        body.pack(fill="both", expand=True)
        self.val = {}
        for key, label in self.ROWS:
            row = tk.Frame(body, bg=t["bg"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label, bg=t["bg"], fg=t["muted"], width=7,
                     anchor="w", font=f9).pack(side="left")
            v = tk.Label(row, text="--", bg=t["bg"], fg=t["text"], anchor="w", font=f9)
            v.pack(side="left", fill="x", expand=True)
            self.val[key] = v

        self.hint = tk.Label(root, text="", bg=t["panel2"], fg=t["gold"], justify="left",
                             wraplength=340, padx=12, pady=7,
                             font=("Microsoft YaHei UI", 9))

        self.foot = tk.Frame(root, bg=t["bg"], padx=12, pady=9)
        self.foot.pack(fill="x")
        tk.Button(self.foot, text="打开日志", command=self._open_log, bg=t["panel"],
                  fg=t["text"], activebackground=t["border"], activeforeground=t["text"],
                  bd=0, font=f9, padx=10, pady=4).pack(side="left")
        tk.Button(self.foot, text="退出", command=self._on_close, bg=t["panel"],
                  fg=t["text"], activebackground=t["close_hover"], activeforeground=t["text"],
                  bd=0, font=f9, padx=14, pady=4).pack(side="right")

        self.refresh()
        self._fit()

    # ---- 布局 ----
    def _fit(self):
        self.root.update_idletasks()
        w = 380
        h = self.root.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{max(0, sw - w - 16)}+{max(0, sh - h - 72)}")

    # ---- 每秒刷新 ----
    def refresh(self):
        app, t = self.app, THEME
        now = datetime.datetime.now()
        trading = is_trade_day(now.date())
        reason = trade_day_reason(now.date())
        state = app.state

        if not trading:
            phase, color = f"{reason} · 无实时数据", t["gold"]
            hint = f"今日{reason}，不会有盘中数据。交易日 09:30–14:57 打开才会提示。"
        elif state == "wait":
            phase, color = f"等待竞价集合（{CONFIG['auction_at'][:5]}）", t["muted"]
            hint = "还没到采集时间：09:26 采集竞价集合，09:30 起监控盘中新上板个股。"
        elif state == "auction":
            phase, color = "竞价集合采集中…", t["cyan"]
            hint = "正在采集竞价涨停股作为去重基准，09:30 起开始监控。"
        elif state == "intraday":
            phase, color = f"盘中监控中 · 每 {CONFIG['poll_interval']} 秒扫描", t["green"]
            hint = ""
        else:
            phase, color = "今日已收盘 · 监控结束", t["muted"]
            hint = "14:57 后停止扫描，可直接关闭窗口退出。"

        self.val["phase"].config(text=phase, fg=color)
        self.val["qdate"].config(text=str(app.fetcher._cache_qdate or "--"))
        self.val["pool"].config(text=(f"{len(app.fetcher._cache)} 只"
                                      if app.fetcher._cache else "--"))
        self.val["notified"].config(text=f"{len(app.dedup.notified)} 只")
        last = app._intraday_last_check
        self.val["scan"].config(
            text=datetime.datetime.fromtimestamp(last).strftime("%H:%M:%S") if last else "--")
        self.dot.config(text="● 运行中" if state != "done" else "● 已结束",
                        fg=t["green"] if state != "done" else t["muted"])

        changed = False
        if hint:
            if self._hint_text != hint:
                self.hint.config(text=hint)
                self._hint_text = hint
            if not self.hint.winfo_ismapped():
                self.hint.pack(fill="x", before=self.foot)
                changed = True
        elif self.hint.winfo_ismapped():
            self.hint.pack_forget()
            self._hint_text = None
            changed = True
        if changed:
            self._fit()

    # ---- 动作 ----
    def _open_log(self):
        if _LOG_FILE and os.path.exists(_LOG_FILE):
            try:
                os.startfile(_LOG_FILE)      # noqa: S606 - Windows 专用，打开日志
            except Exception:  # noqa: BLE001
                pass
        else:
            show_notice_card("暂无日志", "日志文件尚未生成。", ms=2500, root=self.root)

    def _on_close(self):
        log("[退出] 状态窗已关闭")
        self.root.destroy()


# ---------------------------------------------------------------------------
# 调度引擎
# ---------------------------------------------------------------------------
_LOG_FILE = None   # 由 --log-file 设置；pythonw 静默运行时靠文件日志
_MUTEX_HANDLE = None


def app_dir():
    """程序所在目录：源码运行时是脚本目录，PyInstaller 打包后是 exe 目录。

    onefile 模式下 __file__ 落在临时解压目录 _MEIxxxx，退出即被删除，
    所以日志与崩溃记录必须锚定 sys.executable 的目录，否则留不下痕迹。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def asset_path(name):
    """定位随包资源：打包后在 _MEIPASS\assets，源码运行在脚本目录\assets。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, "assets", name)
    return p if os.path.exists(p) else None


def enable_dpi_awareness():
    """高分屏下避免弹窗文字发虚；老系统无 shcore 时降级，失败静默。"""
    try:
        import ctypes
    except Exception:  # noqa: BLE001
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def acquire_single_instance():
    """命名互斥防重复启动：重复双击会让同一只票弹两次。返回是否为本进程首次占用。"""
    global _MUTEX_HANDLE
    try:
        import ctypes
        _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(
            None, False, "Local\\ZTPopup_SingleInstance")
        return ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001
        return True


def show_notice_card(title, detail="", ms=4500, root=None):
    """右下角深色提示卡片，自动关闭。

    root 为空时自建窗口并阻塞到关闭（用于启动早期尚无主窗的场景）；
    传入 root 时以 Toplevel 弹出，不阻塞调用方的事件循环。
    """
    try:
        import tkinter as tk
        t = THEME
        own = root is None
        win = tk.Tk() if own else tk.Toplevel(root)
        win.title(title)
        win.configure(bg=t["border"])
        win.overrideredirect(True)
        win.attributes("-topmost", True)

        body = tk.Frame(win, bg=t["bg"], padx=14, pady=10)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=title, bg=t["bg"], fg=t["gold"],
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        if detail:
            tk.Label(body, text=detail, bg=t["bg"], fg=t["muted"], justify="left",
                     wraplength=330,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))

        win.update_idletasks()
        w = max(body.winfo_reqwidth() + 2, 260)
        h = body.winfo_reqheight() + 2
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{max(0, sw - w - 16)}+{max(0, sh - h - 70)}")
        win.after(ms, win.destroy)
        if own:
            win.mainloop()
    except Exception:  # noqa: BLE001 - 无图形环境时静默
        pass


def alert_already_running():
    """已有实例时的极简提示（1.8 秒自关），避免"双击没反应"的错觉。"""
    show_notice_card("涨停板提示器已在运行",
                     "盘中新上板的个股会继续弹窗，无需重复启动", ms=1800)


def now_str():
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(msg):
    """控制台 + 文件双写；pythonw 无控制台时 print 自动跳过。"""
    line = f"[{now_str()}] {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001 - pythonw 下 sys.stdout 为 None
        pass
    if _LOG_FILE:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass


def parse_hms(s):
    h, m, sec = (int(x) for x in s.split(":"))
    return datetime.time(h, m, sec)


def today_yyyymmdd():
    return datetime.datetime.now().strftime("%Y%m%d")


class MonitorApp:
    """主控：阶段状态机 + 每 1 秒 tick。"""

    def __init__(self, root, notifier, dedup, fetcher):
        self.root = root
        self.notifier = notifier
        self.dedup = dedup
        self.fetcher = fetcher
        self.state = "wait"
        self._auction_done = False
        self._intraday_last_check = None
        self._warmup_done = False   # 盘中启动基线：首次扫描只记录不弹窗，避免补发轰炸
        self.panel = None           # 状态窗（控制台模式为 None）
        self._auction_round = 0
        self._auction_failed = 0
        self._auction_pending = False

    # ---- 核心：阶段推进（GUI 与控制台共用） ----
    def advance(self):
        now = datetime.datetime.now().time()
        a_at = parse_hms(CONFIG["auction_at"])
        i_start = parse_hms(CONFIG["intraday_start"])
        i_end = parse_hms(CONFIG["intraday_end"])

        if self.state == "wait":
            if now >= a_at:
                self.state = "auction"
                self.run_auction()
        elif self.state == "auction":
            if self._auction_pending:
                return          # 采集未完成，暂不推进（等价于原先的同步阻塞，但不卡界面）
            if now >= i_start:
                self.state = "intraday"
                log("[调度] 进入盘中监控阶段"
                    f"（{CONFIG['intraday_start']} 起每 {CONFIG['poll_interval']} 秒扫描新增涨停）")
        elif self.state == "intraday":
            if now >= i_end:
                self.state = "done"
                log("[调度] 盘中监控结束（14:57），当日共提示盘中首板 "
                    f"{len(self.dedup.notified)} 只。Ctrl+C 退出。")
            elif self._intraday_last_check is None or \
                    (time.time() - self._intraday_last_check) >= CONFIG["poll_interval"]:
                self._intraday_last_check = time.time()
                self.run_intraday_scan()

    def tick(self):
        """GUI 版：由 root.after 每 1 秒驱动。"""
        self.advance()
        if self.panel is not None:
            self.panel.refresh()
        self.root.after(1000, self.tick)

    def tick_console(self):
        """控制台版：由外部 while+sleep 驱动。"""
        self.advance()

    # ---- S1 竞价集合采集（不弹 9:26 全景窗，仅用于盘中去重） ----
    def run_auction(self):
        """S1 竞价集合采集：2 次兜底、间隔 20 秒（业务规则同原版）。

        GUI 模式改成 root.after 串行调度：原版在这里同步 sleep 20 秒，
        状态窗可见后会假死 20 秒，所以只把等待方式换成事件驱动，规则不变。
        """
        log(f"[调度] 竞价集合采集（{CONFIG['auction_repeat']} 次兜底，9:26 全景弹窗已停用）")
        self._auction_round = 0
        self._auction_failed = 0
        self._auction_pending = True
        self._auction_once()

    def _auction_once(self):
        i = self._auction_round
        try:
            qdate, stocks = self.fetcher.fetch_pool(today_yyyymmdd())
            if i == 0:
                self.dedup.set_auction_pool(stocks)
            else:
                self.dedup.merge_auction_pool(stocks)
            log(f"[竞价] 第 {i + 1} 次采集完成：涨停池 {len(stocks)} 只，"
                f"竞价涨停 {len(self.dedup.s1)} 只（数据日 {qdate}）")
        except Exception as e:  # noqa: BLE001
            self._auction_failed += 1
            log(f"[竞价] 第 {i + 1} 次采集失败：{e}")

        self._auction_round += 1
        if self._auction_round < CONFIG["auction_repeat"]:
            if self.root is not None:
                self.root.after(CONFIG["auction_repeat_gap"] * 1000, self._auction_once)
            else:
                time.sleep(CONFIG["auction_repeat_gap"])
                self._auction_once()
            return
        self._finish_auction()

    def _finish_auction(self):
        self._auction_pending = False
        s1_list = sorted(self.dedup.s1.values(), key=lambda x: -x.fund)
        if self.notifier is not None and CONFIG["show_auction_panel"]:
            self.notifier.show_auction_panel(s1_list, today_yyyymmdd())
        else:
            log(f"[竞价] 竞价涨停 {len(s1_list)} 只已记入 S1（不弹全景窗，仅作盘中去重基准）")

        # 全失败时给一次可见反馈，否则手动启动后"双击没反应"会让人以为程序没工作
        if self._auction_failed == CONFIG["auction_repeat"] and self.notifier is not None:
            show_notice_card("竞价集合采集失败",
                             "涨停池接口暂时不可用，进入盘中后会继续每 5 秒重试。",
                             ms=5000, root=self.root)

        s1_list = sorted(self.dedup.s1.values(), key=lambda x: -x.fund)
        if self.notifier is not None and CONFIG["show_auction_panel"]:
            self.notifier.show_auction_panel(s1_list, today_yyyymmdd())
        else:
            log(f"[竞价] 竞价涨停 {len(s1_list)} 只已记入 S1（不弹全景窗，仅作盘中去重基准）")

    # ---- 盘中轮询 ----
    def run_intraday_scan(self):
        try:
            _qdate, pool = self.fetcher.fetch_pool(today_yyyymmdd())
        except Exception as e:  # noqa: BLE001
            log(f"[盘中] 轮询失败：{e}")
            return
        fresh = self.dedup.new_intraday(pool)
        if not fresh:
            return
        self.dedup.mark_notified(fresh)  # 先记入，保证单次提示
        # 启动基线：盘中中途启动时，第一次扫描把启动前已涨停的股票记入去重
        # 集合但不弹窗，避免把上午积累的涨停全部补发轰炸；之后新触板才提示。
        if not self._warmup_done:
            self._warmup_done = True
            log(f"[盘中] 启动基线：忽略启动前已涨停 {len(fresh)} 只（不补发弹窗），"
                "从现在起新触板的涨停才提示")
            return
        log(f"[盘中] 新增首板 {len(fresh)} 只："
            + ", ".join(f"{s.code}{s.name}" for s in fresh))
        if self.notifier is not None:
            for s in fresh:
                self.notifier.enqueue_intraday(s)
        else:
            for s in fresh:
                log(f"      >> {s.line()}")


# ---------------------------------------------------------------------------
# 演示模式：用最近交易日真实数据走完整流程
# ---------------------------------------------------------------------------
def run_demo(root, notifier, dedup, fetcher):
    log("[演示] 拉取最近交易日涨停数据…")
    try:
        qdate, pool = fetcher.fetch_pool(today_yyyymmdd())
    except Exception as e:  # noqa: BLE001 - 接口风控/断网时给提示，而不是崩出 traceback
        log(f"[演示] 数据拉取失败：{e}")
        show_notice_card("数据拉取失败",
                         f"涨停池接口暂时不可用（{e}）。\n请稍后重试，或检查网络与代理。",
                         ms=6000, root=root)
        root.after(6200, root.quit)
        return
    if not pool:
        log("[演示] 涨停池为空，无法演示")
        show_notice_card("涨停池为空", "接口返回 0 只，可能处于休市日，请盘后再试。",
                         ms=5000, root=root)
        root.after(5200, root.quit)
        return
    log(f"[演示] 数据日 {qdate}，涨停池共 {len(pool)} 只")

    dedup.set_auction_pool(pool)
    auction = list(dedup.s1.values())
    log(f"[演示] 竞价涨停 {len(auction)} 只已记入 S1（全景弹窗已停用）")

    # 模拟 2 只"盘中首次触板"（演示 S1 排除 + 去重后的单次提示效果）
    demo_picks = [s for s in pool if s.lbc <= 1][:2] or pool[:2]

    def _demo_intraday():
        for i, s in enumerate(demo_picks):
            root.after(3500 + i * 3500, lambda st=s: notifier.enqueue_intraday(st, is_demo=True))
        log(f"[演示] 模拟 {len(demo_picks)} 只盘中首板弹窗（3.5 秒后逐只弹出，3 秒自动关）")

    root.after(3000, _demo_intraday)
    log("[演示] 进行中：单股窗 3 秒自动关。Ctrl+C 退出。")


# ---------------------------------------------------------------------------
# 自检模式：真实数据 + 去重逻辑断言 + GUI 冒烟
# ---------------------------------------------------------------------------
def run_selftest(no_gui):
    failures = []

    def ok(cond, msg):
        print(("  [PASS] " if cond else "  [FAIL] ") + msg)
        if not cond:
            failures.append(msg)
        return cond

    log("== 自检 1/4：真实数据采集与解析 ==")
    fetcher = ZTDataFetcher()
    try:
        qdate, pool = fetcher.fetch_pool(today_yyyymmdd())
    except Exception as e:  # noqa: BLE001 - 数据源不可用时给出明确结论而非 traceback
        print(f"  [FAIL] 涨停池拉取失败：{e}")
        log(f"== 自检中止：数据源不可用（{e}）==")
        sys.exit(1)
    ok(len(pool) > 0, f"涨停池非空：数据日 {qdate}，共 {len(pool)} 只")
    s0 = pool[0]
    ok(s0.code and s0.name, f"字段解析正常：{s0.line()}")
    ok(s0.fund_text and s0.lbc_text and s0.fbt_text, "金额/连板/时间格式化正常")
    auction = [s for s in pool if s.is_auction]
    print(f"  竞价涨停 {len(auction)} 只 / 盘中新增候选 {len(pool) - len(auction)} 只")

    log("== 自检 2/4：去重状态机（竞价集合 S1 排除） ==")
    dedup = DedupManager()
    dedup.set_auction_pool(pool)
    if auction:
        ok(dedup.new_intraday(auction) == [], "竞价涨停股在盘中绝不重复提示（S1 排除）")

    log("== 自检 3/4：去重状态机（首次触板 + 炸板回封抑制） ==")
    non_auction = [s for s in pool if not s.is_auction]
    if non_auction:
        fake = non_auction[:2]
        fresh1 = dedup.new_intraday(fake)
        ok(len(fresh1) == 2, f"首次触板识别 2 只（{', '.join(s.code for s in fresh1)}）")
        dedup.mark_notified(fresh1)
        ok(dedup.new_intraday(fake) == [], "炸板回封不再提示（notified 单次保证）")
        extra = [s for s in non_auction if s.code not in dedup.notified]
        if extra:
            fresh2 = dedup.new_intraday(fake + [extra[0]])
            ok(len(fresh2) == 1 and fresh2[0].code == extra[0].code,
               f"混合池仅提示真正新首板（{extra[0].code}{extra[0].name}）")

    log("== 自检 4/4：GUI 冒烟测试 ==")
    if no_gui:
        print("  [SKIP] --no-gui 模式跳过 GUI 冒烟")
    else:
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            n = ZTNotifier(root)
            n.enqueue_intraday(pool[0], is_demo=True)   # 单股窗入队+3 秒自动关
            root.after(600, root.quit)
            root.mainloop()
            ok(True, "tkinter 弹窗创建/销毁/自动关闭无异常")
        except tk.TclError as e:
            print(f"  [WARN] 当前环境无可用显示（{e}），GUI 冒烟跳过")
        except Exception as e:  # noqa: BLE001
            ok(False, f"GUI 冒烟异常：{e}")

    if failures:
        log(f"== 自检失败 {len(failures)} 项 ==")
        sys.exit(1)
    log("== 自检全部通过 ==")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="A股涨停板提示器：盘中新增涨停提示（9:30-14:57），右下角弹窗",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--demo", action="store_true", help="演示模式：立即用最近交易日真实数据跑完整流程")
    p.add_argument("--selftest", action="store_true", help="自检：真实数据+去重逻辑+GUI 冒烟")
    p.add_argument("--no-gui", action="store_true", help="强制控制台模式（不弹窗，仅日志）")
    p.add_argument("--interval", type=int, default=None, help="盘中轮询间隔（秒，默认 5）")
    p.add_argument("--check-trade-day", action="store_true",
                   help="只打印今天是否 A股交易日（诊断用）后退出")
    p.add_argument("--log-file", default=None, metavar="PATH",
                   help="日志文件路径（默认：exe 同目录 zt_popup.log）")
    p.add_argument("--no-logfile", action="store_true", help="不写日志文件（仅控制台）")
    return p


def _except_hook(exc_type, exc_val, exc_tb):
    """未捕获异常落盘（pythonw 静默运行无控制台，靠此留痕）。"""
    import traceback
    try:
        lines = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
        crash = os.path.join(app_dir(), "zt_crash.log")
        with open(crash, "a", encoding="utf-8") as f:
            f.write(f"[{now_str()}] {lines}\n")
    except Exception:  # noqa: BLE001
        pass
    sys.__excepthook__(exc_type, exc_val, exc_tb)


def main():
    sys.excepthook = _except_hook
    enable_dpi_awareness()
    args = build_parser().parse_args()
    # 单实例：诊断/自检不占锁（可能与正在监控的实例并存）
    if not args.selftest and not args.check_trade_day and not acquire_single_instance():
        alert_already_running()
        return
    if args.interval:
        CONFIG["poll_interval"] = args.interval

    global _LOG_FILE
    if not args.no_logfile:
        _LOG_FILE = args.log_file or os.path.join(app_dir(), "zt_popup.log")

    # 交易日快速诊断
    if args.check_trade_day:
        today = datetime.date.today()
        print(f"今天：{today.strftime('%Y-%m-%d')}（{today.strftime('%A')}）→ "
              f"{trade_day_reason(today)}，{'是' if is_trade_day(today) else '非'}交易日")
        sys.exit(0)

    try:
        import tkinter  # noqa: F401
        gui_ok = True
    except ImportError:
        gui_ok = False
        log("警告：当前 Python 缺少 tkinter，自动进入控制台模式（仅日志，不弹窗）。")
    no_gui = args.no_gui or not gui_ok

    if args.selftest:
        run_selftest(no_gui)
        return

    if args.demo:
        if no_gui:
            log("演示模式需要 GUI（tkinter），当前不可用。可改用 --selftest 验证逻辑。")
            sys.exit(2)
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        n, d, f = ZTNotifier(root), DedupManager(), ZTDataFetcher()
        run_demo(root, n, d, f)
        root.mainloop()
        return

    # 正常监控模式：无条件启动（开机自启常驻，非交易日会在 wait 状态待机，
    # 交易日自动触发。盘中段：S1 竞价集合采集 → 盘中新增提示）
    dedup, fetcher = DedupManager(), ZTDataFetcher()
    if gui_ok and not args.no_gui:
        import tkinter as tk
        root = tk.Tk()
        notifier = ZTNotifier(root)
        app = MonitorApp(root, notifier, dedup, fetcher)
        app.panel = StatusPanel(root, app)   # 状态窗负责"可见/任务栏图标/非盘中提示"
        log("[启动] 涨停板提示器  |  竞价集合采集后即盘中监控  |  "
            f"盘中 {CONFIG['intraday_start']}-{CONFIG['intraday_end']} 每 {CONFIG['poll_interval']} 秒扫描")
        root.after(1000, app.tick)
        try:
            root.mainloop()
        except KeyboardInterrupt:
            log("已退出。")
    else:
        app = MonitorApp(None, None, dedup, fetcher)
        log("[启动] 控制台监控模式  |  竞价集合采集后即盘中监控  |  "
            f"盘中 {CONFIG['intraday_start']}-{CONFIG['intraday_end']} 每 {CONFIG['poll_interval']} 秒扫描")
        try:
            while True:
                app.tick_console()
                time.sleep(1)
        except KeyboardInterrupt:
            log("已退出。")


if __name__ == "__main__":
    main()
