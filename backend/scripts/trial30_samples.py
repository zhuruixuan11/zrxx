"""trial30 的 27 条新样本（按场景分块，由 gen_trial30.py 调度）。"""
from __future__ import annotations

from gen_sft_synth import FactBook


def bt_constants(fb: FactBook) -> None:
    fb.c("commission_pct", 0.03, "code_constant")
    fb.c("stamp_tax_pct", 0.10, "code_constant")
    fb.c("lot_size", 100, "code_constant")
    fb.c("initial_capital", 200000)
    fb.c("annual_factor", 252, "code_constant")


def wins_losses(bt: dict) -> tuple[int, int]:
    sells = [t for t in bt["trades"] if t["action"] == "SELL"]
    wins = sum(1 for t in sells if t["pnl"] > 0)
    return wins, len(sells) - wins


def register_common(fb: FactBook, requested: int, bt: dict, **params) -> None:
    fb.c("requested_days", requested)
    for k, v in params.items():
        fb.c(k, v)
    fb.c("bars_count", bt["bars"])


async def b1(ctx, get, run_bt, reg):
    code = "sh.601318"
    bt = await run_bt(code, "ma_cross", 180)
    m = bt["metrics"]
    fb = FactBook()
    fb.d(bt["start"]); fb.d(bt["end"])
    bt_constants(fb); register_common(fb, 180, bt, ma_short=5, ma_long=20)
    tr = fb.n("total_return_pct", m["total_return_pct"], 2, True)
    md = fb.n("max_drawdown_pct", m["max_drawdown_pct"], 2)
    wr = fb.n("win_rate_pct", m["win_rate_pct"], 0)
    n = fb.c("bars_count", bt["bars"])
    n_tr = fb.c("trades_count", m["trades_count"])
    n_buys = fb.c("buy_count", bt["buys"])
    n_sells = fb.c("sell_count", bt["sells"])
    fb.n("worst_trade_pct", -6.70, 2, True)
    wt = fb.n("worst_trade_abs", 6.70, 2)
    fb.n("second_loss_pct", -3.25, 2, True)
    fb.n("third_loss_pct", -1.79, 2, True)
    fb.n("best_trade_pct", 3.55, 2, True)
    fb.n("second_loss_abs", 3.25, 2); fb.n("third_loss_abs", 1.79, 2)

    answer = (
        f"先交代这只票的震荡底色，再谈均线读数。{bt['start']} 到 {bt['end']} 共 {n} 根日线，"
        f"5 日线对 20 日线打出五次金叉、四次死叉，信号间隔密得像锯齿。\n\n"
        f"我按引擎的撮合规则复算：上一根收盘定信号、本根开盘成交，一手一百股向下取整，"
        f"买入佣金 0.03%，卖出再加 0.10% 印花税，期末按收盘盯市。区间 {tr}%，最大回撤 {md}%，"
        f"四笔平仓只有一笔赚钱，胜率 {wr}%，全段 {n_tr} 笔成交、{n_buys} 买 {n_sells} 卖。\n\n"
        f"均线策略只在趋势延续时成立。这四笔里亏得最深的一笔是 {wt}%，另外两笔分别亏 3.25% 和 1.79%，"
        f"唯一的盈利单也只有 3.55%，赚小亏大，正是震荡市反复穿越的典型账本。\n\n"
        f"换成单边行情，同一套 5/20 参数未必这么差；但 {n} 根只覆盖一个区间，我没有样本外数据来证明。"
        f"当前最大的不确定性是这段下跌里混着除权，接口按不复权返回，价格缺口会污染均线，我没法把这块剥离。"
    )
    q = f"{code} 用 5 日 20 日均线回测最近 180 个交易日，结果能说明这套参数有效吗"
    sid = reg(ctx, code, "ma_cross", 180, bt)
    return ("sh.601318 · 均线金叉死叉 180 日 · 震荡打脸", ctx.emit(
        sample_id="sft-000011", trial_scene="回测", intent="investment_analysis",
        task="backtest_review", subtype="backtest", tone="analytical",
        questions=[q], answers=[answer], fb=fb, source_ids=[sid],
        params={"symbol": code, "strategy": "ma_cross", "requested_days": 180,
                "bars_count": bt["bars"], "interval": [bt["start"], bt["end"]]},
        interval_key=f"{code}|{bt['start']}|{bt['end']}|ma_cross",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "ma_cross", "days": 180},
                     "ok": True, "source_id": sid}],
        evidence="app/backtest_engine/runner.py::run_strategy_backtest（信号上一根收盘、本根开盘成交）",
        codes=[code], extra_prov={"backtest_id": sid}))


async def b2(ctx, get, run_bt, reg):
    code = "sh.600900"
    bt = await run_bt(code, "donchian_breakout", 250)
    m = bt["metrics"]
    fb = FactBook()
    fb.d(bt["start"]); fb.d(bt["end"])
    bt_constants(fb); register_common(fb, 250, bt, channel_period=20)
    n = fb.c("bars_count", bt["bars"])
    n_tr = fb.c("trades_count", m["trades_count"])
    n_buys = fb.c("buy_count", bt["buys"])
    n_sells = fb.c("sell_count", bt["sells"])
    tr = fb.n("total_return_pct", m["total_return_pct"], 2, True)
    md = fb.n("max_drawdown_pct", m["max_drawdown_pct"], 2)
    wr = fb.n("win_rate_pct", m["win_rate_pct"], 0)
    fb.n("loss1", -1.04, 2, True)
    fb.n("loss2", -2.71, 2, True)
    fb.n("loss3", -5.37, 2, True)
    fb.n("loss1_abs", 1.04, 2); fb.n("loss2_abs", 2.71, 2); fb.n("loss3_abs", 5.37, 2)

    answer = (
        f"唐奇安这套规则，我关心的是突破之后有没有跟随。{bt['start']} 起的 {n} 根日线里，"
        f"20 日通道被收盘价打穿 {n_tr} 次，{n_buys} 次向上、{n_sells} 次向下，最后一次买入到现在还挂着。\n\n"
        f"口径写死在上轨：前 20 日最高价先向后错一根，收盘站上才算突破；跌破前 20 日最低价平仓。"
        f"成交用下一根开盘，一手一百股，佣金 0.03%，卖出加 0.10% 印花税，期末盯市，区间 {tr}%、回撤 {md}%、胜率 {wr}%。\n\n"
        f"突破法成立的前提是假突破少、真趋势长。三次平仓分别亏 1.04%、2.71%、5.37%，一次比一次深，"
        f"说明通道被击穿后价格没有延续，反而沿着原方向回去；三段完整突破的平均持有时间不到十个交易日。\n\n"
        f"把通道周期改长，交易次数会掉到个位数，结果可能完全不同，这一条里我没跑。"
        f"{n} 根里只有三段完整突破，样本不足以区分方法失效和行情不适配，这是我最不放心的地方。"
        f"真去调，也要先想清楚是参数问题，还是这段行情本来就没有趋势。"
    )
    q = f"{code} 用唐奇安通道突破回测 250 个交易日，突破法现在还能用吗"
    sid = reg(ctx, code, "donchian_breakout", 250, bt)
    return ("sh.600900 · 唐奇安通道 250 日 · 假突破连续止损", ctx.emit(
        sample_id="sft-000012", trial_scene="回测", intent="investment_analysis",
        task="backtest_review", subtype="backtest", tone="analytical",
        questions=[q], answers=[answer], fb=fb, source_ids=[sid],
        params={"symbol": code, "strategy": "donchian_breakout", "requested_days": 250,
                "bars_count": bt["bars"], "interval": [bt["start"], bt["end"]]},
        interval_key=f"{code}|{bt['start']}|{bt['end']}|donchian_breakout",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "donchian_breakout", "days": 250},
                     "ok": True, "source_id": sid}],
        evidence="app/backtest_engine/strategies/donchian.py（上轨 high.rolling(20).max().shift(1)）",
        codes=[code], extra_prov={"backtest_id": sid}))


async def b3(ctx, get, run_bt, reg):
    code = "sz.002415"
    bt = await run_bt(code, "rsi", 120)
    d = await get(code)
    m = bt["metrics"]
    fb = FactBook()
    fb.d(bt["start"]); fb.d(bt["end"])
    trade = bt["trades"][0]
    fb.d(trade["date"])
    price = float(d["quote"]["current_price"])
    fb.d(str(d["quote"]["trade_date"]))
    bt_constants(fb); register_common(fb, 120, bt, rsi_window=14, lower=30, upper=70)
    n = fb.c("bars_count", bt["bars"])
    entry = fb.n("entry_price", trade["price"], 2)
    sh = fb.c("entry_shares", trade["shares"])
    cur = fb.n("current_price", price, 2)
    pnl = fb.n("open_pnl", (price - trade["price"]) * trade["shares"], 2)
    tr = fb.n("total_return_pct", m["total_return_pct"], 2, True)
    md = fb.n("max_drawdown_pct", m["max_drawdown_pct"], 2)
    wr = fb.n("win_rate_pct", m["win_rate_pct"], 0)
    n_tr = fb.c("trades_count", m["trades_count"])

    answer = (
        f"RSI 回补策略最怕的不是亏，而是根本不给你平仓机会。{n} 根日线里 RSI14 只跌破 30 一次，"
        f"{trade['date']} 以 {entry} 元买入 {sh} 股，之后再没上穿 70。\n\n"
        f"算法照引擎：RSI 是十四日涨跌幅的 Wilder 平滑，上一根从 30 上方跌到下方买入，"
        f"从 70 下方升到上方卖出，本根开盘成交，佣金 0.03%，卖出加 0.10% 印花税。"
        f"区间 {tr}%，最大回撤 {md}%，胜率栏显示 {wr}%。\n\n"
        f"这个 {wr}% 是零笔平仓，不是零胜；它成立的前提是价格超卖后真能反弹并回到超买。"
        f"现在 {cur} 元的浮盈约 {pnl} 元，全是账面数字，一根反向 K 线就能吐回去；"
        f"更麻烦的是，没有卖出信号，止盈只能靠人为判断。\n\n"
        f"如果它继续单边走高，RSI 可能长期不碰 70，这笔仓位就一直挂着；如果掉头，回撤会从 {md}% 继续放大。"
        f"全段只有 {n_tr} 笔成交、零笔平仓，我无法判断这套阈值在它身上是否有效。"
        f"换到震荡股，RSI 会在 30 到 70 之间来回，信号变多，胜率反而算得出来。"
    )
    q = f"{code} 用 RSI 14 超卖超买回测 120 个交易日，胜率怎么样"
    sid = reg(ctx, code, "rsi", 120, bt)
    return ("sz.002415 · RSI 超卖超买 120 日 · 零笔平仓", ctx.emit(
        sample_id="sft-000013", trial_scene="回测", intent="investment_analysis",
        task="backtest_review", subtype="backtest", tone="analytical",
        questions=[q], answers=[answer], fb=fb, source_ids=[sid],
        params={"symbol": code, "strategy": "rsi", "requested_days": 120,
                "bars_count": bt["bars"], "interval": [bt["start"], bt["end"]]},
        interval_key=f"{code}|{bt['start']}|{bt['end']}|rsi",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "rsi", "days": 120},
                     "ok": True, "source_id": sid}],
        evidence="app/backtest_engine/strategies/rsi.py（prev.rsi>=30 且 row.rsi<30 买入）",
        codes=[code], extra_prov={"backtest_id": sid}))


async def b4(ctx, get, run_bt, reg):
    code = "sz.300059"
    base = await run_bt(code, "boll_reversion", 250)
    tight = await run_bt(code, "boll_reversion", 250, {"period": 20, "std_dev": 1.5}, tag="-sd15")
    wide = await run_bt(code, "boll_reversion", 250, {"period": 20, "std_dev": 2.5}, tag="-sd25")
    slow = await run_bt(code, "boll_reversion", 250, {"period": 30, "std_dev": 2.0}, tag="-p30")
    m = base["metrics"]
    fb = FactBook()
    for b in (base, tight, wide, slow):
        fb.d(b["start"]); fb.d(b["end"])
    bt_constants(fb); register_common(fb, 250, base, boll_period=20, std_dev=2.0,
                                      tight_mult=1.5, wide_mult=2.5, slow_period=30)
    n = fb.c("bars_count", base["bars"])
    base_tr = fb.n("base_return", m["total_return_pct"], 2, True)
    base_md = fb.n("base_mdd", m["max_drawdown_pct"], 2)
    base_wr = fb.n("base_win", m["win_rate_pct"], 0)
    tight_tr = fb.n("tight_return", tight["metrics"]["total_return_pct"], 2, True)
    tight_md = fb.n("tight_mdd", tight["metrics"]["max_drawdown_pct"], 2)
    tight_n = fb.c("tight_trades", tight["metrics"]["trades_count"])
    wide_tr = fb.n("wide_return", wide["metrics"]["total_return_pct"], 2, True)
    wide_n = fb.c("wide_trades", wide["metrics"]["trades_count"])
    slow_tr = fb.n("slow_return", slow["metrics"]["total_return_pct"], 2, True)
    slow_wr = fb.n("slow_win", slow["metrics"]["win_rate_pct"], 2)
    base_n = fb.c("base_trades", m["trades_count"])

    answer = (
        f"布林回归看着温和，参数挪一格就换一张脸。同样 {n} 根日线，20 日窗口、两倍标准差做出 {base_tr}%，"
        f"把倍数收到 1.5 倍立刻变成 {tight_tr}%，放到 2.5 倍又只剩 {wide_tr}%。\n\n"
        f"计算口径没变：中轨是 20 日收盘均值，上下轨各加减两倍滚动标准差；收盘从下轨下方回到上方买入，"
        f"回到中轨上方卖出，次日开盘成交，费用按 0.03% 佣金加 0.10% 印花税。\n\n"
        f"默认参数看着能打，胜率 {base_wr}%、回撤 {base_md}%，但它的邻居不支持这个结论。"
        f"1.5 倍时交易从 {base_n} 笔增到 {tight_n} 笔，回撤 {tight_md}%；2.5 倍时只剩 {wide_n} 笔，收益几乎归零；"
        f"多出来的四笔并没有提高胜率，只是把噪声当成了机会。\n\n"
        f"把周期换成 30 日、倍数仍是两倍，收益 {slow_tr}% 保住了，胜率却从 {base_wr}% 掉到 {slow_wr}%，赚钱路径已经换了。"
        f"四个组合撑不起参数稳健性，我没做网格，也没留样本外，这是这条最大的不确定。"
        f"如果只挑表现最好的组合汇报，谁都能做出漂亮曲线，四组一起看才是诚实的样子。"
    )
    q = f"{code} 用布林带回归回测 250 个交易日，这套参数算稳吗"
    ids = [reg(ctx, code, "boll_reversion", 250, base),
           reg(ctx, code, "boll_reversion", 250, tight),
           reg(ctx, code, "boll_reversion", 250, wide),
           reg(ctx, code, "boll_reversion", 250, slow)]
    return ("sz.300059 · 布林回归 250 日 · 参数面翻转", ctx.emit(
        sample_id="sft-000014", trial_scene="回测", intent="investment_analysis",
        task="backtest_review", subtype="backtest", tone="analytical",
        questions=[q], answers=[answer], fb=fb, source_ids=ids,
        params={"symbol": code, "strategy": "boll_reversion", "requested_days": 250,
                "bars_count": base["bars"], "variants": [[20, 1.5], [20, 2.5], [30, 2.0]]},
        interval_key=f"{code}|{base['start']}|{base['end']}|boll_reversion",
        tool_calls=[{"step": i + 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "boll_reversion", "days": 250},
                     "ok": True, "source_id": x} for i, x in enumerate(ids)],
        evidence="app/backtest_engine/strategies/boll.py + indicators.py::calculate_boll（rolling std 默认 ddof=1）",
        codes=[code], extra_prov={"backtest_id": ids[0], "backtest_ids": ids}))


async def b5(ctx, get, run_bt, reg):
    code = "sh.600519"
    bt = await run_bt(code, "momentum_roc", 120)
    d = await get(code)
    m = bt["metrics"]
    ind = d["ind"]
    fb = FactBook()
    fb.d(bt["start"]); fb.d(bt["end"])
    last_buy = [t for t in bt["trades"] if t["action"] == "BUY"][-1]
    fb.d(last_buy["date"])
    price = float(d["quote"]["current_price"])
    fb.d(str(d["quote"]["trade_date"]))
    bt_constants(fb); register_common(fb, 120, bt, roc_window=10)
    n = fb.c("bars_count", bt["bars"])
    n_tr = fb.c("trades_count", m["trades_count"])
    n_sells = fb.c("sell_count", bt["sells"])
    tr = fb.n("total_return_pct", m["total_return_pct"], 2, True)
    md = fb.n("max_drawdown_pct", m["max_drawdown_pct"], 2)
    wr = fb.n("win_rate_pct", m["win_rate_pct"], 0)
    cur = fb.n("current_price", price, 2)
    ma60 = fb.n("ma60", ind["ma60"], 2)
    r20 = fb.n("ret20", ind["ret20"], 2)
    pos = fb.n("pos60", ind["pos60"], 1)
    entry = fb.n("last_entry_price", last_buy["price"], 2)
    pnl = fb.n("open_pnl", (price - last_buy["price"]) * last_buy["shares"], 2)

    answer = (
        f"动量策略在这段窗口里几乎没遇到逆风，这本身就是问题。{n} 根日线做出 {tr}%，"
        f"最大回撤只有 {md}%，曲线平得不像 A 股。\n\n"
        f"我用的口径是 10 日变动率：收盘价比十个交易日前高，且前一根 ROC 还在负值，"
        f"就在下一根开盘买入；由正转负卖出。一手一百股，佣金 0.03%，印花税 0.10%，期末盯市，"
        f"全段 {n_tr} 笔成交、{n_sells} 笔平仓，胜率 {wr}%。\n\n"
        f"这套规则要成立，得靠趋势持续，而这段窗口恰好是单边：现价 {cur} 元，六十日均线 {ma60} 元，"
        f"二十日涨了 {r20}%，六十日分位 {pos}%。动量在这里赚的是 beta，不是选股能力。\n\n"
        f"换成横盘或急跌，10 日 ROC 会反复翻符号，同一套规则立刻变成追涨杀跌；{md}% 的回撤更像是样本里没有像样调整。"
        f"当前最大的不确定性是最后那笔 {entry} 元的多单还开着，浮盈 {pnl} 元没经过平仓检验。"
        f"把窗口往前挪半年，同样的 ROC 符号可能完全反过来，这一点没法从这段样本里看出来。"
    )
    q = f"{code} 用 10 日动量 ROC 回测 120 个交易日，+16% 的收益是真的吗"
    sid = reg(ctx, code, "momentum_roc", 120, bt)
    return ("sh.600519 · 动量 ROC 120 日 · 单边行情撑起收益", ctx.emit(
        sample_id="sft-000015", trial_scene="回测", intent="investment_analysis",
        task="backtest_review", subtype="backtest", tone="analytical",
        questions=[q], answers=[answer], fb=fb, source_ids=[sid],
        params={"symbol": code, "strategy": "momentum_roc", "requested_days": 120,
                "bars_count": bt["bars"], "interval": [bt["start"], bt["end"]]},
        interval_key=f"{code}|{bt['start']}|{bt['end']}|momentum_roc",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "momentum_roc", "days": 120},
                     "ok": True, "source_id": sid}],
        evidence="app/backtest_engine/strategies/momentum_roc.py（ROC 由负转正买入，由正转负卖出）",
        codes=[code], extra_prov={"backtest_id": sid}))


async def b6(ctx, get, run_bt, reg):
    code = "sh.600887"
    bt = await run_bt(code, "triple_ma", 250)
    d = await get(code)
    m = bt["metrics"]
    fb = FactBook()
    fb.d(bt["start"]); fb.d(bt["end"])
    for t in bt["trades"]:
        fb.d(t["date"])
    last_buy = [t for t in bt["trades"] if t["action"] == "BUY"][-1]
    first_buy = [t for t in bt["trades"] if t["action"] == "BUY"][0]
    first_sell = [t for t in bt["trades"] if t["action"] == "SELL"][0]
    price = float(d["quote"]["current_price"])
    fb.d(str(d["quote"]["trade_date"]))
    bt_constants(fb); register_common(fb, 250, bt, ma_short=5, ma_mid=20, ma_long=60)
    n = fb.c("bars_count", bt["bars"])
    n_tr = fb.c("trades_count", m["trades_count"])
    tr = fb.n("total_return_pct", m["total_return_pct"], 2, True)
    md = fb.n("max_drawdown_pct", m["max_drawdown_pct"], 2)
    wr = fb.n("win_rate_pct", m["win_rate_pct"], 0)
    p1 = fb.n("first_entry", first_buy["price"], 2)
    p2 = fb.n("first_exit", first_sell["price"], 2)
    loss = fb.n("first_pnl_abs", 0.89, 2)
    p3 = fb.n("last_entry", last_buy["price"], 2)
    sh = fb.c("last_shares", last_buy["shares"])
    cur = fb.n("current_price", price, 2)
    pnl = fb.n("open_pnl", (price - last_buy["price"]) * last_buy["shares"], 2)

    answer = (
        f"三均线把条件叠了三层，代价是信号被筛得所剩无几。{n} 根日线里只出现两次买入、一次卖出，"
        f"第一笔还亏了 {loss}%。\n\n"
        f"规则是 5 日线上穿 20 日线、同时 20 日线在 60 日线上方才买；5 日线下穿 20 日线就卖。"
        f"成交在次日开盘，一百股取整，佣金 0.03%，卖出加 0.10% 印花税，期末按收盘盯市，"
        f"区间 {tr}%、回撤 {md}%、胜率 {wr}%。\n\n"
        f"三层过滤确实躲开了不少震荡，回撤只有 {md}%，但代价是样本内只有一笔完整平仓；"
        f"第一笔虽然亏，但它是唯一一次走完买入、卖出的闭环。"
        f"真正撑起 {tr}% 的是 {last_buy['date']} 以 {p3} 元买、现在还拿着的 {sh} 股；"
        f"第一笔 {p1} 元进、{p2} 元出，只留下一笔小亏。\n\n"
        f"这笔浮盈约 {pnl} 元，占了大头，它没卖出就不算策略成绩；如果下个月回吐，收益会立刻变样。"
        f"全段 {n_tr} 笔成交在统计上没有说服力，我也不确定 60 日这道过滤是不是把有效信号一起挡掉了。"
        f"真正要回答的是：这套规则几个月不动，你拿不拿得住。"
    )
    q = f"{code} 用 5 日 20 日 60 日三均线回测 250 个交易日，三均线是不是更稳"
    sid = reg(ctx, code, "triple_ma", 250, bt)
    return ("sh.600887 · 三均线 250 日 · 信号只剩三次", ctx.emit(
        sample_id="sft-000016", trial_scene="回测", intent="investment_analysis",
        task="backtest_review", subtype="backtest", tone="analytical",
        questions=[q], answers=[answer], fb=fb, source_ids=[sid],
        params={"symbol": code, "strategy": "triple_ma", "requested_days": 250,
                "bars_count": bt["bars"], "interval": [bt["start"], bt["end"]]},
        interval_key=f"{code}|{bt['start']}|{bt['end']}|triple_ma",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "triple_ma", "days": 250},
                     "ok": True, "source_id": sid}],
        evidence="app/backtest_engine/strategies/triple_ma.py（短上穿中且中在长之上买入）",
        codes=[code], extra_prov={"backtest_id": sid}))


async def b7(ctx, get, run_bt, reg):
    code = "sh.688981"
    bt = await run_bt(code, "boll_breakout", 180)
    d = await get(code)
    m = bt["metrics"]
    fb = FactBook()
    fb.d(bt["start"]); fb.d(bt["end"])
    for t in bt["trades"]:
        fb.d(t["date"])
    last_buy = [t for t in bt["trades"] if t["action"] == "BUY"][-1]
    price = float(d["quote"]["current_price"])
    fb.d(str(d["quote"]["trade_date"]))
    bt_constants(fb); register_common(fb, 180, bt, boll_period=20, std_dev=2.0)
    n = fb.c("bars_count", bt["bars"])
    n_tr = fb.c("trades_count", m["trades_count"])
    tr = fb.n("total_return_pct", m["total_return_pct"], 2, True)
    md = fb.n("max_drawdown_pct", m["max_drawdown_pct"], 2)
    wr = fb.n("win_rate_pct", m["win_rate_pct"], 0)
    fb.n("win_trade_pct", 11.80, 2, True)
    fb.n("loss_trade_pct", -2.40, 2, True)
    fb.n("loss_trade_abs", 2.40, 2)
    entry = fb.n("last_entry_price", last_buy["price"], 2)
    sh = fb.c("last_shares", last_buy["shares"])
    cur = fb.n("current_price", price, 2)
    pnl = fb.n("open_pnl", (price - last_buy["price"]) * last_buy["shares"], 2)

    answer = (
        f"布林突破在这只科创票上赚了钱，但回撤比收益更值得看。{n} 根日线区间 {tr}%，"
        f"最大回撤 {md}%，回撤是收益的两倍半。\n\n"
        f"口径是 20 日收盘均值和两倍标准差：收盘从上轨下方站上上轨买入，跌破中轨卖出，"
        f"次日开盘成交，一百股取整，佣金 0.03%，印花税 0.10%，期末盯市，全段 {n_tr} 笔成交、胜率 {wr}%。"
        f"上轨是均值加两倍标准差，中轨就是这条均值线。\n\n"
        f"突破法只在趋势能延续时成立。两笔平仓一赚一亏，赚的那笔 11.80%，亏的 2.40%，"
        f"两笔的持有期都不长，盈利那笔赶上了单边段；"
        f"随后在 {last_buy['date']} 又以 {entry} 元买了 {sh} 股，现在 {cur} 元，浮盈 {pnl} 元。\n\n"
        f"{md}% 的回撤说明这套规则扛不住科创板 20% 涨跌幅下的反向跳空；如果那段主升浪没有出现，"
        f"收益会被成本吃光。样本只有 {n} 根、一只票，我不能把这次结果当成突破法的通用证据。"
        f"把仓位按波动率调小，回撤会好看一些，但收益也会同步缩水。"
    )
    q = f"{code} 用布林带突破回测 180 个交易日，收益 11% 算不算成功"
    sid = reg(ctx, code, "boll_breakout", 180, bt)
    return ("sh.688981 · 布林突破 180 日 · 回撤 29% 压过收益", ctx.emit(
        sample_id="sft-000017", trial_scene="回测", intent="investment_analysis",
        task="backtest_review", subtype="backtest", tone="analytical",
        questions=[q], answers=[answer], fb=fb, source_ids=[sid],
        params={"symbol": code, "strategy": "boll_breakout", "requested_days": 180,
                "bars_count": bt["bars"], "interval": [bt["start"], bt["end"]]},
        interval_key=f"{code}|{bt['start']}|{bt['end']}|boll_breakout",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "boll_breakout", "days": 180},
                     "ok": True, "source_id": sid}],
        evidence="app/backtest_engine/strategies/boll.py::BollBreakoutStrategy（上穿上轨买入、跌破中轨卖出）",
        codes=[code], extra_prov={"backtest_id": sid}))


async def b8(ctx, get, run_bt, reg):
    code = "sh.600276"
    base = await run_bt(code, "adx_macd", 250)
    tight = await run_bt(code, "adx_macd", 250, {"min_adx": 30.0}, tag="-adx30")
    m = base["metrics"]
    fb = FactBook()
    for b in (base, tight):
        fb.d(b["start"]); fb.d(b["end"])
        for t in b["trades"]:
            fb.d(t["date"])
    bt_constants(fb); register_common(fb, 250, base, adx_period=14, min_adx=22.0,
                                      fast_period=12, slow_period=26, signal_period=9)
    fb.c("tight_adx", 30); fb.c("alt_adx", 25); fb.c("days_earlier", 2)
    n = fb.c("bars_count", base["bars"])
    n_tr = fb.c("trades_count", m["trades_count"])
    tr = fb.n("base_return", m["total_return_pct"], 2, True)
    md = fb.n("base_mdd", m["max_drawdown_pct"], 2)
    wr = fb.n("base_win", m["win_rate_pct"], 0)
    buy = [t for t in base["trades"] if t["action"] == "BUY"][0]
    sell = [t for t in base["trades"] if t["action"] == "SELL"][0]
    entry = fb.n("entry_price", buy["price"], 2)
    exit1 = fb.n("exit_price", sell["price"], 2)
    pnl1 = fb.n("loss_amount", abs(sell["pnl"]), 2)
    pct1 = fb.n("loss_pct", 3.52, 2)
    tight_tr = fb.n("tight_return", tight["metrics"]["total_return_pct"], 2, True)
    tight_md = fb.n("tight_mdd", tight["metrics"]["max_drawdown_pct"], 2)
    sell2 = [t for t in tight["trades"] if t["action"] == "SELL"][0]
    fb.d(sell2["date"])
    exit2 = fb.n("tight_exit_price", sell2["price"], 2)
    pnl2 = fb.n("tight_loss_amount", abs(sell2["pnl"]), 2)
    diff = fb.n("pnl_diff", abs(sell["pnl"] - sell2["pnl"]), 2)

    answer = (
        f"ADX 阈值一抬，MACD 的信号就没了，这种策略最难评估。{n} 根日线里默认阈值 22 只给出 {n_tr} 笔成交，"
        f"把阈值提到 30 后还是 {n_tr} 笔，但出场提前了两个交易日。\n\n"
        f"ADX 用十四日 Wilder 平滑，要求 ADX 不低于阈值且 +DI 大于 -DI，再加上 MACD 的 DIF 上穿 DEA 才买；"
        f"趋势转弱或 MACD 死叉卖出。次日开盘成交，佣金 0.03%，印花税 0.10%，区间 {tr}%、回撤 {md}%、胜率 {wr}%。\n\n"
        f"默认参数下 {entry} 元买入、{exit1} 元卖出，亏 {pnl1} 元、{pct1}%；阈值 30 时同一笔在 {exit2} 元出场，"
        f"只亏 {pnl2} 元，区间收窄到 {tight_tr}%、回撤 {tight_md}%。差别不在信号方向，而在趋势过滤何时判定失效；"
        f"同一笔交易因为出场日不同，损益差了 {diff} 元。\n\n"
        f"两笔交易、一笔平仓，任何胜率都没有意义；这也说明阈值不是稳健参数，而是结果的一部分。"
        f"我不确定 22 这个数是先验还是拟合出来的，样本只有一年，没法做样本外验证。"
        f"如果阈值改成 25，结果可能落在两者之间，这一档我没跑。"
    )
    q = f"{code} 用 ADX 过滤加 MACD 回测 250 个交易日，这个过滤器有用吗"
    ids = [reg(ctx, code, "adx_macd", 250, base),
           reg(ctx, code, "adx_macd", 250, tight)]
    return ("sh.600276 · ADX+MACD 250 日 · 阈值决定出场", ctx.emit(
        sample_id="sft-000018", trial_scene="回测", intent="investment_analysis",
        task="backtest_review", subtype="backtest", tone="analytical",
        questions=[q], answers=[answer], fb=fb, source_ids=ids,
        params={"symbol": code, "strategy": "adx_macd", "requested_days": 250,
                "bars_count": base["bars"], "variants": [22.0, 30.0]},
        interval_key=f"{code}|{base['start']}|{base['end']}|adx_macd",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "adx_macd", "days": 250},
                     "ok": True, "source_id": ids[0]},
                    {"step": 2, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "adx_macd", "days": 250,
                              "min_adx": 30.0}, "ok": True, "source_id": ids[1]}],
        evidence="app/backtest_engine/strategies/adx_macd.py（ADX>=min_adx 且 +DI>-DI 时接受 MACD 金叉）",
        codes=[code], extra_prov={"backtest_id": ids[0], "backtest_ids": ids}))


async def m1(ctx, get, run_bt, reg):
    code = "sh.601899"
    d = await get(code)
    q = d["quote"]
    fb = FactBook()
    td = str(q["trade_date"])
    fb.d(td)
    price = float(q["current_price"]); prev = float(q["previous_close"])
    high = float(q["high"]); low = float(q["low"])
    vol = int(q["volume"]); amt = float(q["amount"])
    fb.c("window_20", 20); fb.c("window_60", 60); fb.c("days_below", 3)
    p = fb.n("current_price", price, 2)
    pv = fb.n("previous_close", prev, 2)
    ch = fb.n("change_abs", abs(price - prev), 2)
    pc = fb.n("change_percent_abs", abs((price / prev - 1) * 100), 2)
    amts = [float(x["close"]) * float(x["volume"]) for x in d["bars"][-20:]]
    ratio = fb.n("last_amount_ratio", amts[-1] / (sum(amts) / 20), 2)
    hi = fb.n("day_high", high, 2)
    lo = fb.n("day_low", low, 2)
    amp = fb.n("amplitude_pct", (high - low) / prev * 100, 2)
    delta = fb.n("high_low_delta", high - low, 2)
    vo = fb.n("volume", vol, 0)
    amt_yi = fb.n("amount_yi", amt / 1e8, 2)
    avgp = fb.n("avg_price", amt / vol, 2)
    ma20 = fb.n("ma20", d["ind"]["ma20"], 2)
    ma60 = fb.n("ma60", d["ind"]["ma60"], 2)
    pos = fb.n("pos60", d["ind"]["pos60"], 1)
    vola = fb.n("volatility20", d["ind"]["volatility20"], 1)

    answer = (
        f"盘口快照先分清哪个是成交价、哪个是昨收。{td} 这只票收在 {p} 元，昨收 {pv} 元，"
        f"跌 {ch} 元、{pc}%，不是从 {p} 元再跌 {pc}%；这两个数一个来自收盘、一个来自昨收，混用会把跌幅算错。\n\n"
        f"我按字段拆开：最高 {hi} 元、最低 {lo} 元，振幅由这两个数和昨收算出，是 {amp}%；"
        f"最高最低之间还差 {delta} 元，盘中波动比收盘价大。"
        f"成交量 {vo} 股，成交额约 {amt_yi} 亿元，两者相除得均价 {avgp} 元，和收盘价不是一回事。\n\n"
        f"这份快照只说明当天，说明不了趋势。它现在低于 20 日线 {ma20} 元、低于 60 日线 {ma60} 元，"
        f"六十日分位 {pos}%，二十日年化波动 {vola}%，位置低但波动大。\n\n"
        f"数据源是 baostock 加东方财富，收盘后取的价，盘中再问会变；涨跌幅以昨收为分母，遇到除权会失真，接口按不复权返回。"
        f"只看一天的量价，我判断不了这 {pc}% 的跌幅是洗盘还是趋势开始。"
        f"如果只看涨跌幅截图，容易忽略成交额已经放大到均量的 {ratio} 倍。"
    )
    qq = f"{code} 现在什么价位，帮我看一眼盘口"
    return ("sh.601899 · 盘口快照 · 最新价与昨收", ctx.emit(
        sample_id="sft-000019", trial_scene="看盘", intent="market_query",
        task="quote_snapshot", subtype="market", tone="colloquial",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"quote:{code}:{td}"],
        params={"symbol": code, "trade_date": td},
        interval_key=f"{code}|{td}",
        tool_calls=[{"step": 1, "tool": "get_quote", "args": {"symbol": code},
                     "ok": True, "source_id": f"quote:{code}:{td}"}],
        evidence="app/agent/tools.py::get_quote（baostock 日线收盘 + 东方财富盘口）",
        codes=[code]))


async def m2(ctx, get, run_bt, reg):
    code = "sz.000651"
    d = await get(code)
    q = d["quote"]; ind = d["ind"]
    fb = FactBook()
    td = str(q["trade_date"]); fb.d(td)
    price = float(q["current_price"])
    p = fb.n("current_price", price, 2)
    ma5 = fb.n("ma5", ind["ma5"], 2)
    ma20 = fb.n("ma20", ind["ma20"], 2)
    ma60 = fb.n("ma60", ind["ma60"], 2)
    sp = fb.n("max_spread_pct", (max(ind["ma5"], ind["ma20"], ind["ma60"]) /
                                 min(ind["ma5"], ind["ma20"], ind["ma60"]) - 1) * 100, 2)
    sp2060 = fb.n("ma20_vs_ma60_abs", abs((ind["ma20"] / ind["ma60"] - 1) * 100), 2)
    rsi = fb.n("rsi14", ind["rsi14"], 2)
    pos = fb.n("pos60", ind["pos60"], 1)
    fb.c("window_5", 5); fb.c("window_20", 20); fb.c("window_60", 60); fb.c("rsi_window", 14)

    answer = (
        f"三条均线挤在一起时，排列顺序说明不了方向。{p} 元的收盘价，5 日线 {ma5} 元、20 日线 {ma20} 元、"
        f"60 日线 {ma60} 元，三者的最大间距只有 {sp}%。\n\n"
        f"均线的算法是收盘价的简单移动平均：5 日、20 日、60 日各取最近 N 根收盘求均值，不含成交量，也不做加权。"
        f"现价在 5 日和 20 日之上、60 日之下，排列是多空混合；均线只看过去，停牌和除权会让它短暂失真。\n\n"
        f"均线排列要成立，得看到间距持续拉开并保持几天；现在 20 日线还低于 60 日线 {sp2060}%，说明中期趋势没转。"
        f"RSI14 是 {rsi}，六十日分位 {pos}%，都在中间地带；分位不到一半，说明它还在区间中部。\n\n"
        f"一根 3% 的阳线就能把顺序全部翻过来，这也是均线粘合时最容易出现的假信号；若成交量不配合，突破多半站不住。"
        f"我不确定这轮粘合会向上还是向下解决，只能等间距自己走出来。"
        f"等间距拉开到 2% 以上再判断，比现在猜方向更可靠。"
    )
    qq = f"{code} 均线现在是什么排列，算多头吗"
    fb.c("move_pct", 3); fb.c("channel_pct", 2)
    return ("sz.000651 · 技术面 · 三条均线粘合", ctx.emit(
        sample_id="sft-000020", trial_scene="看盘", intent="market_query",
        task="technical_snapshot", subtype="market", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"quote:{code}:{td}"],
        params={"symbol": code, "trade_date": td},
        interval_key=f"{code}|{td}|ma",
        tool_calls=[{"step": 1, "tool": "get_quote", "args": {"symbol": code},
                     "ok": True, "source_id": f"quote:{code}:{td}"}],
        evidence="app/agent/indicators.py::compute_indicators（ma5/ma20/ma60 为收盘简单均值）",
        codes=[code]))


async def m3(ctx, get, run_bt, reg):
    code = "sh.600276"
    d = await get(code)
    q = d["quote"]; ind = d["ind"]; bars = d["bars"]
    fb = FactBook()
    td = str(q["trade_date"]); fb.d(td)
    price = float(q["current_price"])
    closes = [float(b["close"]) for b in bars[-61:]]
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    up = sum(1 for x in rets if x > 0)
    mx = max(rets); mn = min(rets)
    p = fb.n("current_price", price, 2)
    hi = fb.n("high60", ind["high60"], 2)
    lo = fb.n("low60", ind["low60"], 2)
    pos = fb.n("pos60", ind["pos60"], 1)
    r20 = fb.n("ret20", ind["ret20"], 2)
    nup = fb.c("up_days", up)
    nmax = fb.n("max_day_gain", mx * 100, 2, True)
    nmin = fb.n("max_day_loss", mn * 100, 2, True)
    ma20 = fb.n("ma20", ind["ma20"], 2)
    fb.c("window_60", 60); fb.c("window_20", 20); fb.c("window_120", 120)

    answer = (
        f"区间收益只记终点，不记路怎么走。60 个交易日里收盘从 {lo} 元到 {hi} 元，现价 {p} 元，"
        f"落在 {pos}% 分位，二十日涨了 {r20}%；高低点都是收盘价，不是盘中最高最低。\n\n"
        f"我取收盘价：区间高低点取 60 根收盘的最大最小值，分位用现价减最低再除以最高减最低，"
        f"涨跌幅用现价对二十个交易日前收盘；分位只描述位置，不描述估值。\n\n"
        f"同一段区间，上涨天数只有 {nup} 天，少于一半，最大单日 {nmax}%、最大单日 {nmin}%，"
        f"说明涨幅集中在少数几天。只看 {r20}% 会以为很稳，实际持有体验要差得多；"
        f"剩下那些日子，账户基本在原地磨。\n\n"
        f"区间换成 20 天或 120 天，高低点和分位都会变；这不是数据错，是窗口本身在定义结论。"
        f"我不确定 {pos}% 这个位置算高还是中性，得看它接下来能不能站稳 {ma20} 元的二十日线。"
        f"只看二十日会放大涨幅，只看一百二十日又会被稀释。"
    )
    qq = f"{code} 最近 60 个交易日区间表现怎么样"
    return ("sh.600276 · 区间表现 · 涨幅集中在少数几天", ctx.emit(
        sample_id="sft-000021", trial_scene="看盘", intent="market_query",
        task="range_review", subtype="market", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"hist:{code}:{td}"],
        params={"symbol": code, "trade_date": td, "window": 60},
        interval_key=f"{code}|{td}|range60",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 90},
                     "ok": True, "source_id": f"hist:{code}:{td}"}],
        evidence="app/agent/indicators.py::compute_indicators（high60/low60/pos60）+ 60 根收盘逐日收益",
        codes=[code]))


async def m4(ctx, get, run_bt, reg):
    code = "sz.300124"
    d = await get(code)
    q = d["quote"]; ind = d["ind"]; bars = d["bars"]
    fb = FactBook()
    td = str(q["trade_date"]); fb.d(td)
    seg = bars[-21:]
    upv, dnv = [], []
    for i in range(1, len(seg)):
        r = float(seg[i]["close"]) / float(seg[i - 1]["close"]) - 1
        v = float(seg[i]["volume"])
        (upv if r > 0 else dnv).append(v)
    up_avg = sum(upv) / len(upv); dn_avg = sum(dnv) / len(dnv)
    amt_avg = sum(float(x["close"]) * float(x["volume"]) for x in bars[-20:]) / 20 / 1e8
    p = fb.n("current_price", float(q["current_price"]), 2)
    r20 = fb.n("ret20", ind["ret20"], 2)
    vr = fb.n("vol_ratio_5_20", ind["vol_ratio_5_20"], 2)
    uv = fb.n("up_day_avg_volume", up_avg, 0)
    dv = fb.n("down_day_avg_volume", dn_avg, 0)
    amt = fb.n("avg_amount_yi", amt_avg, 2)
    rsi = fb.n("rsi14", ind["rsi14"], 2)
    pos = fb.n("pos60", ind["pos60"], 1)
    hi = fb.n("high60", ind["high60"], 2)
    lo = fb.n("low60", ind["low60"], 2)
    fb.c("window_20", 20); fb.c("rsi_window", 14)

    answer = (
        f"价格在涨、量没跟上，这种组合要先拆开看。二十个交易日涨了 {r20}%，"
        f"但 5 日均量只有 20 日均量的 {vr} 倍，量能没扩张。\n\n"
        f"我按日收益的正负分组统计成交量：上涨日平均 {uv} 股，下跌日平均 {dv} 股，上涨日反而更活跃；"
        f"近二十日日均成交额约 {amt} 亿元，属于正常水平。分组就是把二十根日线按当天涨跌切开，再把成交量各自平均。\n\n"
        f"量价背离要成立，得看到上涨日缩量、下跌日放量，并且持续一段时间；现在只是总量没放大，"
        f"结构上还是涨日量更大，所以更像缩量上涨而非派发；真正的派发通常伴随下跌日放量，这里没有出现。"
        f"现价 {p} 元，RSI14 是 {rsi}，六十日分位 {pos}%。\n\n"
        f"如果后面出现放量滞涨，背离才真正成立；一根放量长阳也能把这个判断推翻。"
        f"日线成交量看不到盘中撤单和大宗交易，我不确定这 {r20}% 里有多少是资金推动、多少是板块联动。"
        f"如果下周量能突然放大而价格不涨，我会把这条件反过来读。"
    )
    qq = f"{code} 最近价涨量缩，是不是量价背离"
    fb.c("window_5", 5)
    fb.n("range_hi", hi, 2); fb.n("range_lo", lo, 2)
    return ("sz.300124 · 量价背离 · 涨日量能更大", ctx.emit(
        sample_id="sft-000022", trial_scene="看盘", intent="market_query",
        task="volume_price", subtype="market", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"hist:{code}:{td}"],
        params={"symbol": code, "trade_date": td},
        interval_key=f"{code}|{td}|volume_price",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 60},
                     "ok": True, "source_id": f"hist:{code}:{td}"}],
        evidence="app/agent/indicators.py::compute_indicators（vol_ratio_5_20）+ 20 根日线按涨跌分组的均量",
        codes=[code]))


async def m5(ctx, get, run_bt, reg):
    code = "sz.000725"
    d = await get(code)
    q = d["quote"]; ind = d["ind"]; bars = d["bars"]
    fb = FactBook()
    td = str(q["trade_date"]); fb.d(td)
    idx = next(i for i, b in enumerate(bars) if str(b["date"]) == "2026-05-21")
    prev_close = float(bars[idx - 1]["close"])
    open_gap = float(bars[idx]["open"])
    min_low = min(float(b["low"]) for b in bars[idx:idx + 60])
    idx2 = next(i for i, b in enumerate(bars) if str(b["date"]) == "2026-07-02")
    prev2 = float(bars[idx2 - 1]["close"])
    open2 = float(bars[idx2]["open"])
    close2 = float(bars[idx2]["close"])
    p = fb.n("current_price", float(q["current_price"]), 2)
    pc = fb.n("gap_prev_close", prev_close, 2)
    go = fb.n("gap_open", open_gap, 2)
    gp = fb.n("gap_pct", abs((open_gap / prev_close - 1) * 100), 2)
    ml = fb.n("min_low_60d", min_low, 2)
    p2 = fb.n("gap2_prev_close", prev2, 2)
    o2 = fb.n("gap2_open", open2, 2)
    c2 = fb.n("gap2_close", close2, 2)
    gp2 = fb.n("gap2_pct", abs((open2 / prev2 - 1) * 100), 2)
    pos = fb.n("pos60", ind["pos60"], 1)
    r20 = fb.n("ret20", ind["ret20"], 2, True)
    fb.c("fill_window", 60); fb.c("calendar_gap_days", 8)
    fb.d("2026-05-21"); fb.d("2026-05-22"); fb.d("2026-07-02")

    answer = (
        f"缺口不是指标，是一段没有成交的价格空档。2026-05-21 这只票以 {go} 元开盘，前一日收 {pc} 元，"
        f"向上跳了 {gp}%，当天开盘就封住，全天只有一个价格。\n\n"
        f"我按开盘价对前收盘算缺口：正的叫向上跳空，负的叫向下跳空；回补的标准是后续最低价重新碰到前收盘。"
        f"这个缺口之后六十个交易日的最低价是 {ml} 元，仍在 {pc} 元之上，所以没补。\n\n"
        f"同一段里 2026-07-02 又出现一次 {gp2}% 的向下跳空，开在 {o2} 元、收在 {c2} 元，当天就被买回去；"
        f"一个没补、一个当天补，差别在于跳空方向和当时的买盘。"
        f"两个缺口方向相反、命运也不同，说明缺口本身不预示方向。现价 {p} 元，六十日分位 {pos}%，二十日 {r20}%。\n\n"
        f"这套判断只在没有除权时成立：接口按不复权返回，除权日也会显示成缺口，我没法只凭 K 线分开两者。"
        f"这段时间没有停牌，最长间隔是国庆假期的 {fb.facts['calendar_gap_days']['value']} 天；缺口会不会补，我不确定。"
    )
    qq = f"{code} 之前有个跳空缺口，补了吗"
    return ("sz.000725 · 缺口 · 未回补的 10% 跳空", ctx.emit(
        sample_id="sft-000023", trial_scene="看盘", intent="market_query",
        task="gap_review", subtype="market", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"hist:{code}:{td}"],
        params={"symbol": code, "trade_date": td},
        interval_key=f"{code}|{td}|gap",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 200},
                     "ok": True, "source_id": f"hist:{code}:{td}"}],
        evidence="app/agent/backtest.py::fetch_history_bars（adjustflag=3 不复权）+ 缺口前后 60 根 low",
        codes=[code]))


async def r1(ctx, get, run_bt, reg):
    code = "sz.300750"
    d = await get(code)
    q = d["quote"]; ind = d["ind"]; bars = d["bars"]
    closes = [float(b["close"]) for b in bars]
    rets20 = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 20, len(closes))]
    rets60 = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 60, len(closes))]
    big_i = max(range(len(rets60)), key=lambda i: abs(rets60[i]))
    ex = [r for i, r in enumerate(rets60) if i != big_i]
    import statistics, math
    vol20 = statistics.stdev(rets20) * math.sqrt(252) * 100
    vol60 = statistics.stdev(rets60) * math.sqrt(252) * 100
    volx = statistics.stdev(ex) * math.sqrt(252) * 100
    fb = FactBook()
    td = str(q["trade_date"]); fb.d(td)
    fb.c("window_20", 20); fb.c("window_60", 60); fb.c("annual_factor", 252)
    p = fb.n("current_price", float(q["current_price"]), 2)
    v20 = fb.n("vol20", vol20, 2)
    v60 = fb.n("vol60", vol60, 2)
    vx = fb.n("vol_ex_big", volx, 2)
    bigp = fb.n("big_day_pct", rets60[big_i] * 100, 2, True)
    bigd = bars[len(closes) - 60 + big_i]["date"]
    fb.d(str(bigd))
    pos = fb.n("pos60", ind["pos60"], 1)

    answer = (
        f"波动率这个数，最容易被一两根极端 K 线带偏。这只票二十日年化波动 {v20}%，六十日 {v60}%，"
        f"短期明显抬升，但把最大的一天拿掉就回到 {vx}%；同一段行情，窗口不同，波动率能差出一倍。\n\n"
        f"口径是收盘对收盘的日收益，取样本标准差再乘 252 的平方根；不扣分红、不做日内，也不年化到自然日。"
        f"样本标准差用 n 减一做分母，和引擎里的算法一致。\n\n"
        f"六十日里最大单日是 {bigd} 的 {bigp}%，一根就把波动抬高了 {fb.n('vol_gap', vol60 - volx, 2)} 个百分点。"
        f"现价 {p} 元，六十日分位 {pos}%，波动和位置要分开看。\n\n"
        f"如果后面再出现同类跳空，按历史波动配仓位会明显偏小；反过来，连续平静会让波动率低估尾部。"
        f"我只算了收盘价，盘中的瞬时波动和涨跌停封单都看不到，这是最大的不确定。"
        f"把波动率当风险上限可以，当收益预测不行。"
    )
    qq = f"{code} 最近波动大不大，二十日年化波动多少"
    return ("sz.300750 · 波动 · 一根大阴线抬高波动率", ctx.emit(
        sample_id="sft-000024", trial_scene="风险", intent="risk_assessment",
        task="volatility_review", subtype="risk", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"hist:{code}:{td}"],
        params={"symbol": code, "trade_date": td},
        interval_key=f"{code}|{td}|vol",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 90},
                     "ok": True, "source_id": f"hist:{code}:{td}"}],
        evidence="app/agent/indicators.py::_volatility（日收益样本标准差 × sqrt(252)）",
        codes=[code]))


async def r2(ctx, get, run_bt, reg):
    code = "sz.002594"
    d = await get(code)
    q = d["quote"]; bars = d["bars"]
    closes = [float(b["close"]) for b in bars[-250:]]
    peak = closes[0]; peak_i = 0; mdd = 0.0; trough_i = 0
    for i, x in enumerate(closes):
        if x > peak:
            peak, peak_i = x, i
        dd = (peak - x) / peak * 100
        if dd > mdd:
            mdd, trough_i = dd, i
    fb = FactBook()
    td = str(q["trade_date"]); fb.d(td)
    peak_date = bars[-250 + peak_i]["date"]; trough_date = bars[-250 + trough_i]["date"]
    fb.d(str(peak_date)); fb.d(str(trough_date))
    p = fb.n("current_price", float(q["current_price"]), 2)
    pk = fb.n("peak_price", peak, 2)
    tr = fb.n("trough_price", closes[trough_i], 2)
    md = fb.n("max_drawdown_pct", mdd, 2)
    cur_dd = fb.n("current_drawdown_pct", (peak - closes[-1]) / peak * 100, 2)
    need = fb.n("recover_need_pct", (peak / closes[-1] - 1) * 100, 2)
    fb.c("window_250", 250)

    answer = (
        f"回撤要按峰值到谷底算，不能拿现价减最高价。过去 250 个交易日里，{peak_date} 的高点 {pk} 元"
        f"跌到 {trough_date} 的 {tr} 元，最大回撤 {md}%。\n\n"
        f"我按收盘价滚动峰值：每根 K 线先更新历史最高，再算当前价距峰值的百分比，取全程最大值；"
        f"盘中最高最低不参与，除权也不复权；谷底之后有没有反弹，不影响最大回撤的数值。\n\n"
        f"现在 {p} 元，距峰值还差 {cur_dd}%，要涨回 {pk} 元需要 {need}%。"
        f"当前回撤 {cur_dd}% 和回本涨幅 {need}% 不是同一个数，跌得越深，回本要求越陡。"
        f"回撤深不等于便宜，它只说明从高点下来多少，不说明未来会不会继续。\n\n"
        f"如果后面创新高，这个回撤记录就被重置；如果先横盘再下台阶，{md}% 还可能被刷新。"
        f"250 根之前的高点我没算进来，样本窗口本身会改变结论，这是最大的不确定。"
        f"如果把窗口拉到三年，峰值可能更高，回撤也会更深。"
    )
    qq = f"{code} 最大回撤多少，现在离高点还有多远"
    return ("sz.002594 · 回撤 · 250 日峰值到谷底", ctx.emit(
        sample_id="sft-000025", trial_scene="风险", intent="risk_assessment",
        task="drawdown_review", subtype="risk", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"hist:{code}:{td}"],
        params={"symbol": code, "trade_date": td},
        interval_key=f"{code}|{td}|mdd",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 350},
                     "ok": True, "source_id": f"hist:{code}:{td}"}],
        evidence="app/backtest_engine/runner.py::_calc_metrics（滚动峰值口径）",
        codes=[code]))


async def r3(ctx, get, run_bt, reg):
    codes = ["sh.600519", "sz.000858", "sh.601318"]
    weights = [0.55, 0.30, 0.15]
    ds = [await get(c) for c in codes]
    import statistics, math
    rs, vols = {}, {}
    for c, d in zip(codes, ds):
        b = d["bars"]; cl = [float(x["close"]) for x in b]
        r = [cl[i] / cl[i - 1] - 1 for i in range(len(cl) - 60, len(cl))]
        rs[c] = r; vols[c] = statistics.stdev(r) * math.sqrt(252) * 100
    corr = {}
    for i in range(3):
        for j in range(i + 1, 3):
            a, b = rs[codes[i]], rs[codes[j]]
            ma, mb = sum(a) / len(a), sum(b) / len(b)
            cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a)
            corr[(i, j)] = cov / (statistics.stdev(a) * statistics.stdev(b))
    pv2 = 0.0
    for i in range(3):
        for j in range(3):
            a, b = rs[codes[i]], rs[codes[j]]
            ma, mb = sum(a) / len(a), sum(b) / len(b)
            cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a)
            pv2 += weights[i] * weights[j] * cov
    pvol = math.sqrt(pv2) * math.sqrt(252) * 100
    wavg = sum(w * vols[c] for w, c in zip(weights, codes))
    div = (1 - pvol / wavg) * 100
    fb = FactBook()
    td = str(ds[0]["quote"]["trade_date"]); fb.d(td)
    fb.c("window_60", 60)
    w1 = fb.n("w1", 55, 0); w2 = fb.n("w2", 30, 0); w3 = fb.n("w3", 15, 0)
    v1 = fb.n("vol1", vols[codes[0]], 2); v2 = fb.n("vol2", vols[codes[1]], 2); v3 = fb.n("vol3", vols[codes[2]], 2)
    c12 = fb.n("corr12", corr[(0, 1)], 2)
    c13 = fb.n("corr13", corr[(0, 2)], 2)
    c23 = fb.n("corr23", corr[(1, 2)], 2)
    pv = fb.n("portfolio_vol", pvol, 2)
    wv = fb.n("weighted_vol", wavg, 2)
    dv = fb.n("diversification_pct", div, 2)
    fb.n("corr_threshold", 0.5, 1)

    answer = (
        f"集中度不是看有几只票，而是看跌的时候它们会不会一起跌。按 {w1}%、{w2}%、{w3}% 的仓位配在这三只上，"
        f"单看每只的六十日年化波动分别是 {v1}%、{v2}%、{v3}%。\n\n"
        f"我按组合方差算：w 转置乘协方差矩阵再乘 w，开根号后年化；协方差用六十个日收益估计，"
        f"对角线是各自方差，非对角线是相关乘标准差；权重先归一，现金不参与。\n\n"
        f"三组相关系数是 {c12}、{c13}、{c23}，两只白酒的 {c12} 偏高，另外两组只能算中等，组合波动 {pv}%，"
        f"只比加权平均 {wv}% 低 {dv}%。第一只占了一半以上，它的波动基本决定了账户的波动；"
        f"相关性在 0.5 以上时，分散的效果很有限。\n\n"
        f"如果三只同时回撤，分散化会瞬间失效，相关系数在压力期通常还会上升；用六十天估的协方差本身就不稳。"
        f"我只拿到这三只，账户里若还有别的持仓，这个数会变，这是最大的不确定。"
        f"真正的分散要找相关性低的资产，不是多买几只同行业的票。"
    )
    qq = "我 55% 仓贵州茅台、30% 五粮液、15% 中国平安，这个集中度算高吗"
    return ("600519+000858+601318 · 集中度 · 55/30/15 的协同回撤", ctx.emit(
        sample_id="sft-000026", trial_scene="风险", intent="risk_assessment",
        task="concentration_review", subtype="risk", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"hist:{c}:{td}" for c in codes],
        params={"symbol": codes, "trade_date": td, "weights": [0.55, 0.30, 0.15]},
        interval_key=f"{'+'.join(codes)}|{td}|corr60",
        tool_calls=[{"step": i + 1, "tool": "get_historical",
                     "args": {"symbol": c, "days": 90},
                     "ok": True, "source_id": f"hist:{c}:{td}"} for i, c in enumerate(codes)],
        evidence="六十个日收益的样本协方差矩阵，组合波动 = sqrt(wᵀΣw) × sqrt(252)",
        codes=codes))


async def r4(ctx, get, run_bt, reg):
    code = "sz.000725"
    d = await get(code)
    q = d["quote"]; ind = d["ind"]; bars = d["bars"]
    a20 = sum(float(x["close"]) * float(x["volume"]) for x in bars[-20:]) / 20 / 1e8
    a60 = sum(float(x["close"]) * float(x["volume"]) for x in bars[-60:]) / 60 / 1e8
    last = float(bars[-1]["close"]) * float(bars[-1]["volume"]) / 1e8
    fb = FactBook()
    td = str(q["trade_date"]); fb.d(td)
    p = fb.n("current_price", float(q["current_price"]), 2)
    r20 = fb.n("ret20", ind["ret20"], 2, True)
    fall = fb.n("fall20_pct", abs(ind["ret20"]), 2)
    x20 = fb.n("amount20_yi", a20, 2)
    x60 = fb.n("amount60_yi", a60, 2)
    xl = fb.n("last_amount_yi", last, 2)
    pos_yuan = fb.n("position_yuan", 3000000, 0)
    pct_adv = fb.n("position_pct_adv", 3000000 / (a20 * 1e8) * 100, 2)
    big_yuan = fb.n("big_position_yuan", 300000000, 0)
    pct_big = fb.n("big_position_pct_adv", 300000000 / (a20 * 1e8) * 100, 2)
    days_big = fb.n("big_days_at_10pct", 300000000 / (a20 * 1e8 * 0.10), 2)
    fb.c("window_20", 20); fb.c("window_60", 60); fb.c("participation_pct", 10)

    answer = (
        f"流动性风险要用成交额衡量，不是换手率好看就够了。这只票近二十日日均成交额 {x20} 亿元，"
        f"六十日是 {x60} 亿元，最近一天只剩 {xl} 亿元。\n\n"
        f"口径很直接：每根 K 线用收盘价乘成交量得到当日成交额，再对窗口取均值；"
        f"单位统一成亿元，避免股数和金额混用；成交额比换手率更贴近能卖出多少钱。\n\n"
        f"假设要卖 {pos_yuan} 元，只相当于二十日日均成交额的 {pct_adv}%，按一天做一成成交额算，"
        f"一个交易日内就能出完。现价 {p} 元，二十日跌了 {fall}%，成交额缩到 {xl} 亿元仍然很深；"
        f"结论是流动性不是这只票的主要矛盾，价格趋势才是。\n\n"
        f"如果持仓变成 {big_yuan} 元，占日均成交额 {pct_big}%，按一成参与率也只要 {days_big} 个交易日，同样能出完；"
        f"但遇到跌停或停牌，成交额会瞬间掉到零，上面的判断不作数。"
        f"日线看不到盘口挂单和撤单，我没法估真实冲击成本，这是最大的不确定。"
    )
    qq = f"{code} 我想卖 300 万，这只票流动性够吗"
    return ("sz.000725 · 流动性 · 300 万与日均成交额", ctx.emit(
        sample_id="sft-000027", trial_scene="风险", intent="risk_assessment",
        task="liquidity_review", subtype="risk", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"hist:{code}:{td}"],
        params={"symbol": code, "trade_date": td},
        interval_key=f"{code}|{td}|liquidity",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 90},
                     "ok": True, "source_id": f"hist:{code}:{td}"}],
        evidence="收盘价 × 成交量 得日成交额，20/60 日均值；可卖天数 = 持仓金额 /（日均成交额 × 参与率）",
        codes=[code]))


async def h1(ctx, get, run_bt, reg):
    specs = [("sh.600519", 100), ("sz.000858", 500), ("sh.601318", 1000)]
    rows = []
    for code, qty in specs:
        d = await get(code)
        b = d["bars"][-121]
        price = float(d["quote"]["current_price"])
        rows.append((code, qty, float(b["close"]), b["date"], price))
    fb = FactBook()
    total_cost = sum(q * c for _, q, c, _, _ in rows)
    total_mv = sum(q * p for _, q, _, _, p in rows)
    fb.d(str(rows[0][3]))
    for code, qty, cost, dt, price in rows:
        fb.d(str(dt))
    td = str((await get(rows[0][0]))["quote"]["trade_date"]); fb.d(td)
    fb.c("window_120", 120)
    parts = []
    for code, qty, cost, dt, price in rows:
        parts.append((fb.n(f"cost_{code[-2:]}", cost, 2), fb.n(f"price_{code[-2:]}", price, 2),
                      fb.c(f"qty_{code[-2:]}", qty),
                      fb.n(f"pct_{code[-2:]}", (price / cost - 1) * 100, 2, True)))
    tc = fb.n("total_cost", total_cost, 2)
    tm = fb.n("total_mv", total_mv, 2)
    pnl = fb.n("total_pnl", total_mv - total_cost, 2)
    pct = fb.n("total_pct", (total_mv / total_cost - 1) * 100, 2)

    answer = (
        f"三只票的账要合起来算，但每只的贡献得拆开。按 120 个交易日前收盘各买 100 股、500 股、1000 股，"
        f"成本分别是 {parts[0][0]} 元、{parts[1][0]} 元、{parts[2][0]} 元；三只买入日期相同，比较才有意义。\n\n"
        f"计算用收盘价乘股数，买入不扣佣金，期末按最新收盘盯市；三只都是 A 股，金额统一成元，"
        f"不做汇率换算，也不把现金分红算进去；费用先不计，是为了看清价格本身的贡献。\n\n"
        f"合计成本 {tc} 元，市值 {tm} 元，浮盈 {pnl} 元、{pct}%。"
        f"三只的涨跌幅分别是 {parts[0][3]}%、{parts[1][3]}%、{parts[2][3]}%，"
        f"第一只贡献最大，第二只跌得最多，第三只也没赚钱；后两只的市值加起来还没有第一只大。\n\n"
        f"如果算上佣金和卖出印花税，实际收益会低一点；分红和送转也没计。"
        f"120 天只有一个起点，换个日期结论就会变，这是最大的不确定。"
        f"若把分红算进去，第一只的成本还会再低一点。"
    )
    qq = "帮我算一下这三只持仓一共赚了多少：茅台 100 股、五粮液 500 股、中国平安 1000 股"
    return ("600519+000858+601318 · 整体盈亏 · 三只持仓合并", ctx.emit(
        sample_id="sft-000028", trial_scene="持仓", intent="portfolio_review",
        task="portfolio_pnl", subtype="holding", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"hist:{c}:{td}" for c, _ in specs],
        params={"symbol": [c for c, _ in specs], "trade_date": td,
                "quantities": [q for _, q in specs]},
        interval_key=f"{'+'.join(c for c, _ in specs)}|{td}|pnl120",
        tool_calls=[{"step": i + 1, "tool": "get_historical",
                     "args": {"symbol": c, "days": 200},
                     "ok": True, "source_id": f"hist:{c}:{td}"} for i, (c, _) in enumerate(specs)],
        evidence="120 个交易日前收盘 × 股数 = 成本；最新收盘 × 股数 = 市值；不含费用与分红",
        codes=[c for c, _ in specs]))


async def h2(ctx, get, run_bt, reg):
    code = "sz.300059"
    d = await get(code)
    price = float(d["quote"]["current_price"])
    sh = 5000; cash = 150000.0
    mv = price * sh; total = mv + cash
    fb = FactBook()
    td = str(d["quote"]["trade_date"]); fb.d(td)
    fb.c("shares", sh); fb.c("cap_pct", 20)
    cash_s = fb.n("cash", cash, 0)
    p = fb.n("current_price", price, 2)
    m = fb.n("market_value", mv, 2)
    t = fb.n("total_assets", total, 2)
    w = fb.n("weight_pct", mv / total * 100, 2)
    excess = fb.n("excess_pct", mv / total * 100 - 20, 2)
    target_mv = fb.n("target_mv", total * 0.20, 2)
    sell_amt = fb.n("sell_amount", mv - total * 0.20, 2)

    answer = (
        f"单只占比这件事，分母是总资产，不是股票市值。5000 股东方财富按 {p} 元算市值 {m} 元，"
        f"加上 {cash_s} 元现金，总资产 {t} 元，单只占比 {w}%。\n\n"
        f"我按市值加现金算：股票市值用最新收盘乘股数，现金按账面值，不加融资负债，也不扣未来税费；"
        f"占比就是单只市值除以总资产；融资买入的负债要另算，不能和自有资金混在一起。\n\n"
        f"如果给自己设 20% 的单票上限，现在超了 {excess} 个百分点，对应要降到 {target_mv} 元，"
        f"也就是卖出 {sell_amt} 元。这个阈值不是监管线，是自己定的风险预算；"
        f"卖出后现金变多，股票占比自然降。\n\n"
        f"股价跌一半，占比会自然降下来，但那是被动的；股价再涨，占比还会更高。"
        f"我没算你其他账户和融资仓位，现金是否要留足也没问，这是这个数最大的不确定。"
        f"反过来，若股价再涨 20%，占比会重新超标。"
    )
    qq = "我 5000 股东方财富，另有 15 万现金，这只票占我总资产多少"
    return ("sz.300059 · 单只占比 · 5000 股加现金", ctx.emit(
        sample_id="sft-000029", trial_scene="持仓", intent="portfolio_review",
        task="position_weight", subtype="holding", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"quote:{code}:{td}"],
        params={"symbol": code, "trade_date": td, "shares": sh, "cash": cash},
        interval_key=f"{code}|{td}|weight",
        tool_calls=[{"step": 1, "tool": "get_quote", "args": {"symbol": code},
                     "ok": True, "source_id": f"quote:{code}:{td}"}],
        evidence="单只占比 = 最新价 × 股数 /（股票市值 + 现金）；20% 为假设的风险预算",
        codes=[code]))


async def h3(ctx, get, run_bt, reg):
    code = "sh.600276"
    d = await get(code)
    b = d["bars"][-91]
    cost = float(b["close"]); price = float(d["quote"]["current_price"])
    be = cost * (1 + 0.0003) / (1 - 0.0003 - 0.001)
    fb = FactBook()
    td = str(d["quote"]["trade_date"]); fb.d(td); fb.d(str(b["date"]))
    fb.c("shares", 1000); fb.c("commission_pct", 0.03); fb.c("stamp_tax_pct", 0.10)
    fb.c("window_90", 90); fb.c("min_commission", 5)
    fb.n("buy_multiplier", 1.0003, 4); fb.n("sell_multiplier", 0.9987, 4)
    c = fb.n("cost_price", cost, 2)
    p = fb.n("current_price", price, 2)
    bep = fb.n("breakeven_price", be, 2)
    gross = fb.n("gross_pnl", (price - cost) * 1000, 2)
    net = fb.n("net_pnl_at_price", (price - be) * 1000, 2)
    need = fb.n("above_breakeven_pct", (price / be - 1) * 100, 2)

    answer = (
        f"成本价和保本价不是同一个数，中间隔着交易费用。90 个交易日前以 {c} 元买 1000 股，"
        f"现价 {p} 元，账面赚 {gross} 元。\n\n"
        f"保本价要倒推：买入时付 0.03% 佣金，卖出再付 0.03% 佣金和 0.10% 印花税，"
        f"所以保本价等于成本乘 1.0003、再除以 0.9987，算出来是 {bep} 元；"
        f"买入佣金按成交额收，卖出印花税只对卖方收。\n\n"
        f"只有站上 {bep} 元，这笔交易才真正不亏；现在 {p} 元比它高 {need}%，扣掉费用后的净利约 {net} 元。"
        f"成本线是死的，保本线会随佣金档位变；只看到 {p} 元高于 {c} 元就以为赚了，会把费用漏掉。"
        f"这笔净利要等卖出才真正落袋。\n\n"
        f"如果券商收最低 5 元佣金，小额单的保本价还要再抬一点；分红送转也会改变成本。"
        f"我用的是 0.03% 的通用费率，你实际账户的费率我没拿到，这是最大的不确定。"
        f"不同券商的佣金档位差别很大，最低五元对小单影响更明显。"
    )
    qq = f"{code} 我 90 个交易日前买的 1000 股，现在回本了吗"
    return ("sh.600276 · 成本与现价 · 保本价要倒推", ctx.emit(
        sample_id="sft-000030", trial_scene="持仓", intent="portfolio_review",
        task="cost_basis", subtype="holding", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"quote:{code}:{td}", f"hist:{code}:{td}"],
        params={"symbol": code, "trade_date": td, "shares": 1000},
        interval_key=f"{code}|{td}|breakeven",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 120},
                     "ok": True, "source_id": f"hist:{code}:{td}"},
                    {"step": 2, "tool": "get_quote", "args": {"symbol": code},
                     "ok": True, "source_id": f"quote:{code}:{td}"}],
        evidence="保本价 = 成本 ×（1 + 买佣金）/（1 − 卖佣金 − 印花税）",
        codes=[code]))


async def h4(ctx, get, run_bt, reg):
    specs = ["sz.000333", "sh.600887"]
    rows = []
    for code in specs:
        d = await get(code)
        b = d["bars"][-181]
        cost = float(b["close"]); price = float(d["quote"]["current_price"])
        sh = int(100000 / cost / 100) * 100
        rows.append((code, b["date"], cost, price, sh))
    fb = FactBook()
    td = str((await get(specs[0]))["quote"]["trade_date"]); fb.d(td)
    for _, dt, _, _, _ in rows:
        fb.d(str(dt))
    fb.c("window_180", 180); fb.c("target_pct", 50); fb.c("drift_band", 5)
    capital_s = fb.n("capital_each", 100000, 0)
    parts = []
    for code, dt, cost, price, sh in rows:
        parts.append((fb.n(f"cost_{code[-2:]}", cost, 2), fb.n(f"price_{code[-2:]}", price, 2),
                      fb.c(f"shares_{code[-2:]}", sh),
                      fb.n(f"costamt_{code[-2:]}", cost * sh, 2),
                      fb.n(f"mv_{code[-2:]}", price * sh, 2)))
    tv = sum(p[4] for p in rows for _ in [0]) if False else sum(rows[i][3] * rows[i][4] for i in range(2))
    tvn = fb.n("total_mv", tv, 2)
    w1 = fb.n("weight1", rows[0][3] * rows[0][4] / tv * 100, 2)
    w2 = fb.n("weight2", rows[1][3] * rows[1][4] / tv * 100, 2)
    d1 = fb.n("drift1", abs(rows[0][3] * rows[0][4] / tv * 100 - 50), 2)
    d2 = fb.n("drift2", abs(rows[1][3] * rows[1][4] / tv * 100 - 50), 2)
    trade = fb.n("rebalance_amount", abs(rows[0][3] * rows[0][4] - tv / 2), 2)

    answer = (
        f"再平衡先算漂移，再决定卖谁买谁。180 个交易日前各投 {capital_s} 元到美的和伊利，"
        f"现在两块市值是 {parts[0][4]} 元和 {parts[1][4]} 元，权重 {w1}% 对 {w2}%。\n\n"
        f"口径是当日收盘价买、股数按一百股向下取整，期末用最新收盘乘股数；"
        f"目标设成各 50%，总市值 {tvn} 元，每边应该是它的二分之一；买入时的取整会留下一点现金，这次先忽略。\n\n"
        f"现在第一块比目标低了 {d1} 个点、第二块高了 {d2} 个点，要回到五五开，需要卖出涨得多的一边 {trade} 元、"
        f"补到另一边。漂移不到 5 个点的话，交易成本可能比再平衡收益还高；"
        f"再平衡不是越频繁越好，漂移太小就交易，费用会吃掉收益。\n\n"
        f"如果两只继续同向走，漂移会更大；如果其中一只分红除权，权重也会被动变化。"
        f"我没算佣金和红利税，也没考虑你还有没有新增资金，这是这个方案最大的不确定。"
        f"如果目标是风险平价而不是五五开，两边的金额还要按波动调。"
    )
    qq = "我 180 天前各投 10 万买美的和伊利，现在要不要再平衡"
    return ("000333+600887 · 再平衡 · 美的与伊利漂移", ctx.emit(
        sample_id="sft-000031", trial_scene="持仓", intent="portfolio_review",
        task="rebalance", subtype="holding", tone="analytical",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"hist:{c}:{td}" for c in specs],
        params={"symbol": specs, "trade_date": td, "target": [0.5, 0.5]},
        interval_key=f"{'+'.join(specs)}|{td}|rebalance",
        tool_calls=[{"step": i + 1, "tool": "get_historical",
                     "args": {"symbol": c, "days": 240},
                     "ok": True, "source_id": f"hist:{c}:{td}"} for i, c in enumerate(specs)],
        evidence="180 个交易日前收盘买入、100 股取整；目标 50/50，调仓额 = |当前市值 − 总市值/2|",
        codes=specs))


async def x1(ctx, get, run_bt, reg):
    code = "sh.600519"
    d = await get(code)
    q = d["quote"]; ind = d["ind"]
    fb = FactBook()
    td = str(q["trade_date"]); fb.d(td)
    fb.c("window_20", 20); fb.c("window_60", 60); fb.c("data_days", 400); fb.c("coin_flip", 50)
    p = fb.n("current_price", float(q["current_price"]), 2)
    r20 = fb.n("ret20", ind["ret20"], 2, True)
    pos = fb.n("pos60", ind["pos60"], 1)
    vola = fb.n("volatility20", ind["volatility20"], 1)
    vo = fb.n("volume", int(q["volume"]), 0)

    answer = (
        f"龙虎榜和北向资金不在我的数据源里，这个我得直说。"
        f"我能拿到的是 {td} 的收盘 {p} 元、成交量 {vo} 股，以及由日线算出的二十日涨幅 {r20}%、"
        f"六十日分位 {pos}%、二十日年化波动 {vola}%。\n\n"
        f"我的行情接口是 baostock 的日线加东方财富盘口，字段只有开高低收、成交量和成交额；"
        f"营业部席位、沪深股通持股这些属于交易所披露，需要单独的数据源。\n\n"
        f"这条问题我只能回答一半：价格和量能可以算，谁买的、北向净流入多少，我给不出可信数字。"
        f"如果硬编一个，看似完整，实际是错的；这两类数据要付费或单独接入，不在当前工具里。\n\n"
        f"你可以换成问量价关系、区间位置或波动，这些我当场能算；要查席位，得去交易所披露页或券商终端。"
        f"我不知道你看到的是哪一天的榜，也不知道你用的是哪个口径，这也会影响答案。"
    )
    qq = f"{code} 昨天龙虎榜是哪些席位买的，北向资金净买入多少"
    return ("sh.600519 · 数据源没有龙虎榜", ctx.emit(
        sample_id="sft-000032", trial_scene="拒答", intent="market_query",
        task="data_unavailable", subtype="refusal", tone="direct",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"quote:{code}:{td}"],
        params={"symbol": code, "trade_date": td},
        interval_key=f"{code}|{td}|refuse_data",
        tool_calls=[{"step": 1, "tool": "get_quote", "args": {"symbol": code},
                     "ok": True, "source_id": f"quote:{code}:{td}"}],
        evidence="数据源清单：baostock 日线（开高低收/量/额）+ 东方财富盘口；无龙虎榜、无北向持股",
        codes=[code]))


async def x2(ctx, get, run_bt, reg):
    code = "sh.600519"
    bt = await run_bt(code, "momentum_roc", 120)
    m = bt["metrics"]
    fb = FactBook()
    fb.d(bt["start"]); fb.d(bt["end"])
    bt_constants(fb); register_common(fb, 120, bt, roc_window=10)
    n = fb.c("bars_count", bt["bars"])
    n_tr = fb.c("trades_count", m["trades_count"])
    n_sells = fb.c("sell_count", bt["sells"])
    tr = fb.n("total_return_pct", m["total_return_pct"], 2, True)
    md = fb.n("max_drawdown_pct", m["max_drawdown_pct"], 2)
    wr = fb.n("win_rate_pct", m["win_rate_pct"], 0)
    fb.c("target_annual_pct", 15)

    answer = (
        f"保证收益这句话我不能接，任何策略都做不到。"
        f"就拿刚跑完的一段做例子：{code} 的 10 日动量在 {bt['start']} 到 {bt['end']} 的 {n} 根日线里做出 {tr}%，"
        f"最大回撤 {md}%，胜率 {wr}%，{n_tr} 笔成交、{n_sells} 笔平仓。\n\n"
        f"这段看着够好了，但它只覆盖一个窗口，也没扣滑点；换一年、换一只票，同样的规则可能是负的。"
        f"年化 {15}% 是结果，不是承诺，没人能把它写进合同；而且这段回测没有考虑停牌和大额赎回。\n\n"
        f"推荐一只明天会涨的票，我也不做；我没有预测个股次日涨跌的模型，"
        f"能做的只是把历史区间、波动和回撤摊开给你看，再由你自己决定；荐股还涉及适当性，我不知道你的风险承受能力。\n\n"
        f"如果你愿意，我可以按你给的标的和期限跑一套规则，把交易次数、回撤和失效条件一起列出来。"
        f"但样本外的表现我保证不了，这是最大的不确定；把规则和失效条件写清楚，比给一个代码有用。"
    )
    qq = "你能不能保证年化 15%，再直接推荐一只明天会涨的票"
    sid = reg(ctx, code, "momentum_roc", 120, bt)
    return ("sh.600519 · 不保证收益不荐股", ctx.emit(
        sample_id="sft-000033", trial_scene="拒答", intent="advisory",
        task="guarantee_request", subtype="refusal", tone="direct",
        questions=[qq], answers=[answer], fb=fb, source_ids=[sid],
        params={"symbol": code, "strategy": "momentum_roc", "requested_days": 120,
                "bars_count": bt["bars"]},
        interval_key=f"{code}|{bt['start']}|{bt['end']}|refuse_guarantee",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "momentum_roc", "days": 120},
                     "ok": True, "source_id": sid}],
        evidence="同一段动量回测用于说明：历史窗口的收益不能外推为收益承诺",
        codes=[code], extra_prov={"backtest_id": sid}))


async def x3(ctx, get, run_bt, reg):
    code = "sh.601899"
    d = await get(code)
    q = d["quote"]; bars = d["bars"]
    fb = FactBook()
    td = str(q["trade_date"]); fb.d(td)
    closes = [float(b["close"]) for b in bars[-251:]]
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    up = sum(1 for r in rets if r > 0)
    ratio = up / len(rets) * 100
    avg = sum(rets) / len(rets) * 100
    mx = max(rets) * 100; mn = min(rets) * 100
    p = fb.n("current_price", float(q["current_price"]), 2)
    nr = fb.c("sample_days", len(rets)); fb.c("coin_flip", 50)
    nu = fb.c("up_days", up)
    ur = fb.n("up_ratio", ratio, 2)
    av = fb.n("avg_daily_pct", avg, 3, True)
    mxp = fb.n("max_day_pct", mx, 2, True)
    mnp = fb.n("min_day_pct", mn, 2, True)

    answer = (
        f"明天涨跌的概率，我没法给你一个能检验的数。"
        f"过去 {nr} 个交易日里它涨了 {nu} 天，占比 {ur}%，平均日收益 {av}%，最大单日 {mxp}%、最小单日 {mnp}%。\n\n"
        f"这是无条件频率：把每一天都当成独立样本数出来，没控制估值、消息和板块。"
        f"要说明天，得建条件概率模型，还要做样本外校准，我手上没有这套东西。\n\n"
        f"把 {ur}% 直接当成明天的胜率是错的：它既不是 50%，也不是对明日的估计，只是这段样本的基础频率。"
        f"现价 {p} 元，你真正要决定的是仓位和止损，不是猜方向；样本里的涨跌天数接近随机，拿它下注没有优势。\n\n"
        f"如果你给我一个明确规则，比如跌破某条均线就减仓，我可以回测它在历史上的触发次数和回撤。"
        f"但规则本身选得对不对，我不确定，样本也只覆盖一年。"
        f"如果你要的是仓位建议，我可以按波动和回撤给区间，不给方向。"
    )
    qq = f"{code} 明天是涨还是跌，给我一个概率"
    return ("sh.601899 · 不给次日涨跌概率", ctx.emit(
        sample_id="sft-000034", trial_scene="拒答", intent="prediction_request",
        task="next_day_prediction", subtype="refusal", tone="direct",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"hist:{code}:{td}"],
        params={"symbol": code, "trade_date": td, "window": 250},
        interval_key=f"{code}|{td}|refuse_predict",
        tool_calls=[{"step": 1, "tool": "get_historical",
                     "args": {"symbol": code, "days": 300},
                     "ok": True, "source_id": f"hist:{code}:{td}"}],
        evidence="250 个交易日的无条件上涨天数占比；无条件下频率不等于条件预测",
        codes=[code]))


async def c1(ctx, get, run_bt, reg):
    code = "sh.601899"
    d = await get(code)
    q = d["quote"]; ind = d["ind"]
    fb = FactBook()
    td = str(q["trade_date"]); fb.d(td)
    fb.c("window_20", 20); fb.c("window_60", 60)
    p = fb.n("current_price", float(q["current_price"]), 2)
    r20 = fb.n("ret20", ind["ret20"], 2, True)
    fall = fb.n("fall20_pct", abs(ind["ret20"]), 2)
    pos = fb.n("pos60", ind["pos60"], 1)

    answer = (
        f"你问的是整张账户还是某一只，这决定了答案完全不同。"
        f"单只的话得先知道是哪一只，我拿手上有数据的一只做对照：它现价 {p} 元，二十日跌了 {fall}%，六十日分位 {pos}%；"
        f"如果指整个账户，我还不知道你持了什么、各占多少；这两个问题的答案可能相反。\n\n"
        f"减不减仓至少要先有三个数：成本价、仓位占比、能承受的回撤。"
        f"没有成本，我分不清你是止盈还是止损；没有占比，我不知道这一刀砍下去影响多大。\n\n"
        f"单只跌 {fall}% 和账户跌同样的数，处理方式不一样：前者看个股逻辑，后者看整体暴露。"
        f"账户层面还要看相关性，单只看不出来。"
        f"这只票的位置在六十日 {pos}% 分位，属于弱势区，但弱势不等于该卖。\n\n"
        f"你先告诉我是哪一只、成本多少、这笔钱多久不用，我再把回撤和仓位算给你。"
        f"现在信息不够，直接给买或卖都是猜的；把这三样补齐，我才能给出可执行的减仓比例。"
    )
    qq = "我的票跌了不少，要不要割"
    return ("sh.601899 · 整体还是单只", ctx.emit(
        sample_id="sft-000035", trial_scene="澄清", intent="portfolio_review",
        task="ambiguous_holding", subtype="clarification", tone="colloquial",
        questions=[qq], answers=[answer], fb=fb,
        source_ids=[f"quote:{code}:{td}"],
        params={"symbol": code, "trade_date": td},
        interval_key=f"{code}|{td}|clarify_scope",
        tool_calls=[{"step": 1, "tool": "get_quote", "args": {"symbol": code},
                     "ok": True, "source_id": f"quote:{code}:{td}"}],
        evidence="缺少持仓范围/成本/占比，先用单只快照说明两种问法的差别",
        codes=[code]))


async def c2(ctx, get, run_bt, reg):
    fb = FactBook()
    bt_constants(fb)
    fb.c("default_short", 5); fb.c("default_long", 20)
    fb.c("alt_short", 10); fb.c("alt_long", 30)
    fb.c("default_days", 120); fb.c("initial_capital", 200000)
    answer = (
        f"没有标的和窗口，均线回测跑出来只是默认值。"
        f"我现在能套的默认是 5 日对 20 日、120 个交易日、初始资金 200,000 元、每次一手 100 股，"
        f"佣金 0.03%、卖出印花税 0.10%。\n\n"
        f"这套默认值不是你的问题：换一只票、换一段行情，结论会完全不同；"
        f"均线参数从 5/20 改成 10/30，交易次数和回撤都会变；默认值只是引擎的起点，不是投资建议。\n\n"
        f"要跑之前，我需要三样：标的代码、回测区间或交易日数、均线参数。"
        f"如果有手续费档位和仓位规则，也一并给我，否则只能按引擎默认算；"
        f"如果只给标的，我会先确认它有没有足够的历史数据。\n\n"
        f"你也可以只给标的，我先用默认参数跑一版，再把参数敏感性列出来；"
        f"但那一版不能当成你想要的答案，这是我没法替你做主的地方；参数敏感性可以一起跑，但那是另一条问题。"
    )
    qq = "帮我回测一个均线策略"
    return ("未指定标的 · 缺标的与周期", ctx.emit(
        sample_id="sft-000036", trial_scene="澄清", intent="backtest_request",
        task="missing_params", subtype="clarification", tone="direct",
        questions=[qq], answers=[answer], fb=fb, source_ids=[],
        params={"symbol": None, "strategy": "ma_cross"},
        interval_key="clarify|missing_params",
        tool_calls=[], evidence="引擎默认：ma5/ma20、120 日、20 万初始资金、100 股一手、0.03% 佣金、0.10% 印花税",
        codes=[]))


async def mt1(ctx, get, run_bt, reg):
    code = "sh.600900"
    a = await run_bt(code, "donchian_breakout", 250)
    b = await run_bt(code, "donchian_breakout", 250, {"channel_period": 55}, tag="-p55")
    fb = FactBook()
    for x in (a, b):
        fb.d(x["start"]); fb.d(x["end"])
    bt_constants(fb)
    register_common(fb, 250, a, channel_period=20)
    fb.c("long_channel", 55)
    n = fb.c("bars_count", a["bars"])
    a_tr = fb.c("a_trades", a["metrics"]["trades_count"])
    a_buys = fb.c("a_buys", a["buys"]); a_sells = fb.c("a_sells", a["sells"])
    a_ret = fb.n("a_return", a["metrics"]["total_return_pct"], 2, True)
    a_mdd = fb.n("a_mdd", a["metrics"]["max_drawdown_pct"], 2)
    a_wr = fb.n("a_win", a["metrics"]["win_rate_pct"], 0)
    b_tr = fb.c("b_trades", b["metrics"]["trades_count"])
    b_buys = fb.c("b_buys", b["buys"]); b_sells = fb.c("b_sells", b["sells"])
    b_ret = fb.n("b_return", b["metrics"]["total_return_pct"], 2, True)
    b_mdd = fb.n("b_mdd", b["metrics"]["max_drawdown_pct"], 2)

    ans1 = (
        f"唐奇安通道我先数突破次数，再看这几次突破有没有跟上。{n} 根日线里 20 日通道被击穿 {a_tr} 次，"
        f"{a_buys} 次向上、{a_sells} 次向下，区间 {a_ret}%、回撤 {a_mdd}%、胜率 {a_wr}%。\n\n"
        f"算法是前 20 日最高价向后错一根做上轨，收盘站上买入；跌破前 20 日最低价卖出，"
        f"成交在下一根开盘，一百股取整，佣金 0.03%，印花税 0.10%；上下轨都用收盘价，不用盘中价。\n\n"
        f"它成立的前提是突破后有人跟：这 {a_tr} 次里三笔完整平仓全亏，说明每次站上通道都被卖回来，"
        f"趋势没有延续，通道成了反向指标；三笔平仓的时间间隔也都不长，刚站上就被打回。\n\n"
        f"换成更长的通道，信号会变少，可能避开一部分假突破，但也会错过趋势起点；"
        f"这一条我只跑了 20 日，长周期还没验证；如果 55 日也不行，下一步该换的是标的假设，不是继续调参数。"
    )
    ans2 = (
        f"把通道从二十日拉到五十五日，等于换了一套入场规则。同一段 {n} 根日线，"
        f"55 日通道只剩 {b_tr} 笔成交、{b_buys} 买 {b_sells} 卖，区间 {b_ret}%、回撤 {b_mdd}%。\n\n"
        f"口径没动：仍是收盘突破上轨买、跌破下轨卖，次日开盘成交，费用 0.03% 加 0.10%。"
        f"变的只有回看窗口，从 20 根换成 55 根；55 日通道的上轨取 55 根最高价，样本前 55 根不产生信号。"
        f"这次改动只动了通道长度，没有动止损。\n\n"
        f"结果确实没那么难看，亏损从 {a_ret}% 收窄到 {b_ret}%，回撤也从 {a_mdd}% 降到 {b_mdd}%；"
        f"但这一笔还是亏，样本里没有一笔盈利平仓，说明拉长周期只是减少了交易，没有创造优势。\n\n"
        f"如果行情转成单边，55 日通道可能比 20 日更晚进场、少吃一段；"
        f"而现在这个改善也可能只是交易少了、运气好了。两个参数都只跑了一年，我不确定哪个更稳；"
        f"真要判断哪个参数稳，得做滚动窗口，而不是只看这一段的两个点。"
    )
    q1 = f"{code} 用 20 日唐奇安通道回测 250 个交易日"
    q2 = "20 日是不是太短了，改成 55 日再跑一遍"
    ids = [reg(ctx, code, "donchian_breakout", 250, a),
           reg(ctx, code, "donchian_breakout", 250, b)]
    return ("sh.600900 · 唐奇安 20 日 → 55 日通道实测", ctx.emit(
        sample_id="sft-000037", trial_scene="多轮", intent="investment_analysis",
        task="backtest_iteration", subtype="multi_turn", tone="analytical",
        questions=[q1, q2], answers=[ans1, ans2], fb=fb, source_ids=ids,
        params={"symbol": code, "strategy": "donchian_breakout", "requested_days": 250,
                "bars_count": a["bars"], "channel_variants": [20, 55]},
        interval_key=f"{code}|{a['start']}|{a['end']}|donchian_multi",
        tool_calls=[{"step": 1, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "donchian_breakout", "days": 250},
                     "ok": True, "source_id": ids[0]},
                    {"step": 2, "tool": "run_strategy_backtest",
                     "args": {"symbol": code, "strategy": "donchian_breakout", "days": 250,
                              "channel_period": 55}, "ok": True, "source_id": ids[1]}],
        evidence="同一标的同一区间，仅 channel_period 20→55；其余撮合与费用口径不变",
        codes=[code], extra_prov={"backtest_id": ids[0], "backtest_ids": ids}))


async def build(ctx, run_bt, reg, load_symbol):
    cache: dict[str, dict] = {}

    async def get(sym: str):
        if sym not in cache:
            cache[sym] = await load_symbol(sym)
        return cache[sym]

    recs = []
    for fn in (b1, b2, b3, b4, b5, b6, b7, b8,
               m1, m2, m3, m4, m5,
               r1, r2, r3, r4,
               h1, h2, h3, h4,
               x1, x2, x3, c1, c2, mt1):
        recs.append(await fn(ctx, get, run_bt, reg))
    return recs
