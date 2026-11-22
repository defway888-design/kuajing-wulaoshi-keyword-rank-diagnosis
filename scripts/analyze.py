"""Cross-agent, offline Xiyou daily coverage analysis. Python 3.10+, stdlib only."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sys


CATEGORIES = (
    "自然持续出现词", "自然观察词", "广告持续出现词", "广告观察词",
    "同日双端覆盖词", "交替覆盖词", "双端未观测到", "数据不足",
)
OBSERVED, ABSENT, UNKNOWN = "observed", "not_observed", "unknown"
SIDES = ("organic", "ad")
CONDITIONAL_DIRECTION = (
    "若尚未投放该关键词，考虑新增投放；若已经投放但未观测到广告展示，"
    "核查投放状态和展示条件后，考虑提高竞价。"
)
AD_CHECKS = ["核查 Listing 文字覆盖", "核查关键词与产品相关性"]
CONNECTION_PREFIX = "跨境吴老师当前无法继续自然排名与广告展示诊断："
CONFIGURE_HINT = (
    "请在当前 AI 工具支持的 MCP/连接设置中，按西柚官方接入说明完成配置、授权并启用；"
    "确认工具可见后重新运行。不要在聊天中提交密钥。"
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def obj(value, label):
    require(isinstance(value, dict), f"{label} must be an object")
    return value


def sequence(value, label):
    require(isinstance(value, list), f"{label} must be a list")
    return value


def text_value(value, label):
    require(isinstance(value, str), f"{label} must be a string")
    return value.strip()


def boolean(value, label):
    require(isinstance(value, bool), f"{label} must be boolean")
    return value


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, f"{label} must be an integer >= {minimum}")
    return value


def iso_date(value, label):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value),
            f"{label} must be an ISO date")
    return date.fromisoformat(value)


def check_connection(source):
    """Validate host-supplied preflight evidence; does not contact an MCP server."""
    connection = source.get("connection")
    require(isinstance(connection, dict), CONNECTION_PREFIX + "缺少本次西柚 MCP 接入检查记录。" + CONFIGURE_HINT)
    status = connection.get("status")
    if status == "not_configured":
        raise ValueError(CONNECTION_PREFIX + "当前 AI 工具尚未配置西柚 MCP。" + CONFIGURE_HINT)
    if status == "unauthorized":
        raise ValueError(CONNECTION_PREFIX + "西柚 MCP 尚未授权或权限不足。请在当前工具中完成授权并重新检查。")
    if status == "unavailable":
        raise ValueError(CONNECTION_PREFIX + "西柚 MCP 已配置但当前连接不可用。请检查连接、启用状态及权限后重新运行。")
    require(status == "connected", CONNECTION_PREFIX +
            "当前会话未确认可用的西柚 MCP；未配置时请先配置，已配置时请检查启用、授权和会话加载状态。")
    evidence = connection.get("evidence")
    checked_at = connection.get("checked_at")
    require(isinstance(evidence, str) and bool(evidence.strip()),
            CONNECTION_PREFIX + "缺少本次接入成功证据，请重新检查西柚 MCP，不能直接填入通过状态。")
    checked_time = None
    if isinstance(checked_at, str):
        try:
            checked_time = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        except ValueError:
            pass
    require(checked_time is not None and checked_time.tzinfo is not None and checked_time.utcoffset() is not None,
            CONNECTION_PREFIX + "缺少带时区的本次接入检查时间，请重新检查西柚 MCP。")
    require(connection.get("daily_keywords_available") is True,
            CONNECTION_PREFIX + "未确认日级反查能力可访问。请检查西柚接口版本、权限和能力开放情况。")


def unique(items):
    seen, result = set(), []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def classify(states, adequate=True, ambiguous=False):
    """Classification is independent of relevance or recommendations."""
    require(bool(states), "states must contain at least one day")
    allowed = {OBSERVED, ABSENT, UNKNOWN}
    require(all(len(s) == 2 and s[0] in allowed and s[1] in allowed for s in states),
            "invalid daily state")
    if not adequate or ambiguous or any(UNKNOWN in s for s in states):
        return "数据不足"
    n = len(states)
    organic = sum(s[0] == OBSERVED for s in states)
    ad = sum(s[1] == OBSERVED for s in states)
    both = sum(s[0] == OBSERVED and s[1] == OBSERVED for s in states)
    if both:
        return "同日双端覆盖词"
    if organic and ad:
        return "交替覆盖词"
    if organic:
        return "自然持续出现词" if organic == n else "自然观察词"
    if ad:
        return "广告持续出现词" if ad == n else "广告观察词"
    return "双端未观测到"


def normalize_day(day):
    day = obj(day, "daily item")
    iso_date(day.get("date"), "daily.date")
    entries = sequence(day.get("entries", []), "entries")
    normalized = []
    for entry in entries:
        obj(entry, "entry")
        original = entry.get("keyword")
        keyword = text_value(original, "keyword")
        require(bool(keyword), "keyword cannot be empty")
        flags = obj(entry.get("unknown", {}), "entry.unknown")
        for side in SIDES:
            boolean(flags.get(side, False), f"unknown.{side}")
        positions = sequence(entry.get("positions", []), "positions")
        for position in positions:
            obj(position, "position")
            require(position.get("kind") in (*SIDES, "other", "unknown"), "invalid position kind")
            boolean(position.get("valid", False), "position.valid")
            text_value(position.get("source_type", ""), "position.source_type")
            for field in ("total_rank", "page_rank", "page"):
                if position.get(field) is not None:
                    integer(position[field], field, 1)
            require(position.get("captured_at") is None or isinstance(position["captured_at"], str),
                    "captured_at must be string or null")
        normalized.append({**entry, "keyword": keyword, "raw_keyword": original})

    gaps = []
    completion = day.get("completion", "unknown")
    require(completion in ("complete", "unknown", "pending"), "invalid completion state")
    if completion != "complete" or not text_value(day.get("completion_evidence", ""), "completion_evidence"):
        gaps.append("日期完成状态缺少确认依据")
    date_verified = boolean(day.get("date_verified", False), "date_verified")
    date_evidence = text_value(day.get("date_evidence", ""), "date_evidence")
    if not date_verified or not date_evidence:
        gaps.append("数据日归属未确认")
    paging = obj(day.get("pagination", {}), "pagination")
    complete = boolean(paging.get("complete", False), "pagination.complete")
    requested = sequence(paging.get("requested", []), "pagination.requested")
    received = sequence(paging.get("received", []), "pagination.received")
    for identifiers in (requested, received):
        require(all(type(v) in (str, int) and str(v).strip() for v in identifiers), "invalid page identifier")
    if not complete or not text_value(paging.get("evidence", ""), "pagination.evidence"):
        gaps.append("分页终止或完整性未确认")
    if not requested or requested != unique(requested) or received != unique(received):
        gaps.append("页码/游标缺失或重复")
    if requested != received:
        gaps.append("请求分页与成功分页不一致")
    raw_count = paging.get("received_rows")
    if raw_count is None:
        gaps.append("未提供实际原始行数")
    else:
        integer(raw_count, "received_rows")
        if raw_count != len(entries):
            gaps.append("实际原始行数与标准化行数不一致")
    total = paging.get("reported_total")
    if total is not None:
        integer(total, "reported_total")
        if total != len(entries):
            gaps.append("源总行数与取得行数不一致")
    deduplicated = unique(normalized)
    by_keyword = {}
    for row in deduplicated:
        by_keyword.setdefault(row["keyword"], []).append(row)
    return {
        "date": day["date"], "entries": deduplicated, "by_keyword": by_keyword, "raw_rows": len(entries),
        "ready": not gaps, "identity_verified": date_verified and bool(date_evidence),
        "gaps": gaps, "completion": completion,
        "completion_evidence": day.get("completion_evidence", ""),
        "date_evidence": date_evidence, "pagination": paging,
    }


def make_window(payload, n):
    window = obj(payload.get("window"), "window")
    initial = iso_date(window.get("initial_end"), "window.initial_end")
    end = iso_date(window.get("end"), "window.end")
    shift = (initial - end).days
    require(0 <= shift <= 7, "window shift must be between 0 and 7 days")
    if shift:
        require(window.get("shift_reason") == "confirmed_update_lag"
                and bool(text_value(window.get("shift_evidence", ""), "shift_evidence")),
                "window shift requires confirmed update-lag evidence")
    dates = [(end - timedelta(days=i)).isoformat() for i in reversed(range(n))]
    return {**window, "dates": dates, "shift_days": shift,
            "observation_label": "单日观测" if n == 1 else "连续多日观测"}


def product_available(payload):
    product = obj(payload.get("product", {}), "product")
    return (bool(text_value(product.get("title", ""), "product.title"))
            and bool(text_value(product.get("evidence", ""), "product.evidence")))


def relevance_for(keyword, payload):
    available = product_available(payload)
    mapping = obj(payload.get("relevance", {}), "relevance")
    entry = obj(mapping.get(keyword, {}), "relevance item")
    status = entry.get("status", "review")
    require(status in ("related", "review", "unrelated"), "invalid relevance status")
    reason = text_value(entry.get("reason", ""), "relevance.reason")
    evidence = text_value(entry.get("evidence", ""), "relevance.evidence")
    if not available:
        return {"status": "review", "reason": "商品信息不可得或缺少来源，相关性待核查", "evidence": ""}
    if status != "review" and not (reason and evidence):
        return {"status": "review", "reason": "相关性判断依据不足", "evidence": evidence}
    return {"status": status, "reason": reason or "相关性待核查", "evidence": evidence}


def position_semantics_known(position, source):
    return (position.get("kind") in (*SIDES, "other") and position.get("valid", False)
            and bool(position.get("source_type", "").strip())
            and bool(source.get("mapping_evidence", "").strip()))


def day_fact(keyword, day, source):
    rows = day["by_keyword"].get(keyword, [])
    positions = unique([p for row in rows for p in row.get("positions", [])])
    ambiguous = {side: any(r.get("unknown", {}).get(side, False) for r in rows) for side in SIDES}
    present = {side: False for side in SIDES}
    mapping_known = bool(source.get("mapping_evidence", "").strip())
    date_known = (day["identity_verified"] and bool(source.get("date_basis_evidence", "").strip())
                  and bool(source.get("timezone", "").strip()))
    for position in positions:
        kind = position["kind"]
        if kind == "unknown":
            ambiguous = dict.fromkeys(SIDES, True)
        elif kind in SIDES:
            if position_semantics_known(position, source):
                present[kind] = True
            else:
                ambiguous[kind] = True
    states = {}
    for side in SIDES:
        if date_known and present[side]:
            states[side] = OBSERVED
        elif (not ambiguous[side] and day["ready"] and date_known and mapping_known
              and bool(source.get("coverage", {}).get(side, "").strip())):
            states[side] = ABSENT
        else:
            states[side] = UNKNOWN
    return {"date": day["date"], **states, "ambiguous": ambiguous,
            "positions": positions, "source_rows": rows}


def advice_for(category, relevance):
    if category == "自然持续出现词":
        if relevance["status"] == "related":
            return [CONDITIONAL_DIRECTION]
        if relevance["status"] == "unrelated":
            return ["有明确不相关依据，不进入投放建议清单"]
        return ["需人工核查相关性，暂不建议新增投放或提高竞价"]
    if category == "广告持续出现词":
        return list(AD_CHECKS)
    if category == "广告观察词":
        return ["仅部分日期观测到广告，纳入观察清单", *AD_CHECKS]
    if category in ("自然观察词", "同日双端覆盖词", "交替覆盖词"):
        return ["保留覆盖事实并继续观察，不据此自动调整广告"]
    if category == "数据不足":
        return ["仅展示已有事实及数据缺口，不生成核心建议"]
    return ["采集范围内双端均未观测到，不生成投放建议"]


def analyze(payload):
    obj(payload, "input")
    source = obj(payload.get("source", {}), "source")
    check_connection(source)
    require(type(payload.get("schema_version")) is int and payload["schema_version"] == 2, "unsupported schema_version; current version is 2")
    asin = text_value(payload.get("asin"), "asin").upper()
    country = text_value(payload.get("country"), "country").upper()
    require(re.fullmatch(r"[A-Z0-9]{10}", asin), "asin must contain 10 letters/digits")
    require(re.fullmatch(r"[A-Z]{2}", country), "country must be a verified two-letter source code")
    n = integer(payload.get("days", 3), "days", 1)
    require(source.get("name") == "xiyou", "this contract only supports xiyou")
    source_gaps = []
    for field in ("timezone", "date_basis_evidence", "mapping_evidence"):
        if not text_value(source.get(field, ""), f"source.{field}"):
            source_gaps.append(f"数据源缺少 {field}")
    coverage = obj(source.get("coverage", {}), "coverage")
    for side in SIDES:
        if not text_value(coverage.get(side, ""), f"coverage.{side}"):
            source_gaps.append(f"{side} 采集覆盖范围未确认")
    source_ready = not source_gaps
    window = make_window(payload, n)
    daily = {}
    for raw in sequence(payload.get("daily", []), "daily"):
        day = normalize_day(raw)
        require(day["date"] in window["dates"], "daily contains a date outside the selected window")
        require(day["date"] not in daily, "daily contains a duplicate date")
        daily[day["date"]] = day
    for selected in window["dates"]:
        if selected not in daily:
            daily[selected] = normalize_day({"date": selected})
            daily[selected]["gaps"].insert(0, "该日数据缺失，未使用其他日期替代")
    ordered_days = [daily[d] for d in window["dates"]]
    window_complete = source_ready and all(d["ready"] for d in ordered_days)
    keywords = sorted({r["keyword"] for d in ordered_days for r in d["entries"]})
    results, core_natural, core_ad = [], [], []
    counts = Counter({name: 0 for name in CATEGORIES})
    daily_counts = {d: Counter() for d in window["dates"]}
    for keyword in keywords:
        facts = [day_fact(keyword, d, source) for d in ordered_days]
        states = [(f["organic"], f["ad"]) for f in facts]
        ambiguity = any(any(f["ambiguous"].values()) for f in facts)
        category = classify(states, window_complete, ambiguity)
        counts[category] += 1
        relevance = relevance_for(keyword, payload)
        gaps = []
        if not window_complete:
            gaps.append("整体窗口不完整，见数据覆盖说明")
        for fact in facts:
            for side in SIDES:
                if fact[side] == UNKNOWN or fact["ambiguous"][side]:
                    gaps.append(f"{fact['date']} {side} 状态未知或字段有歧义")
                if fact[side] == OBSERVED:
                    daily_counts[fact["date"]][side] += 1
        item = {
            "keyword": keyword, "category": category, "daily": facts,
            "organic_days": sum(s[0] == OBSERVED for s in states),
            "ad_days": sum(s[1] == OBSERVED for s in states),
            "both_days": sum(s == (OBSERVED, OBSERVED) for s in states),
            "organic_not_observed_days": sum(s[0] == ABSENT for s in states),
            "ad_not_observed_days": sum(s[1] == ABSENT for s in states),
            "relevance": relevance, "directions": advice_for(category, relevance),
            "scope": coverage, "gaps": gaps,
        }
        results.append(item)
        if category == "自然持续出现词" and relevance["status"] == "related":
            core_natural.append(keyword)
        if category == "广告持续出现词":
            core_ad.append(keyword)
    require(sum(counts.values()) == len(keywords), "internal classification count mismatch")
    results.sort(key=lambda r: (CATEGORIES.index(r["category"]), r["keyword"]))
    partial = not window_complete or counts["数据不足"] > 0
    has_product = product_available(payload)
    overall_partial = partial or (not has_product and counts["自然持续出现词"] > 0)
    core_status = {}
    for key, values in (("natural", core_natural), ("ad_only", core_ad)):
        if not window_complete:
            core_status[key] = "数据不足，无法判定"
        elif values:
            core_status[key] = "已列出符合条件的关键词" + ("；另有数据不足词尚无法判定" if partial else "")
        elif partial:
            core_status[key] = "已判定词中无符合条件者；另有数据不足词无法判定"
        elif key == "natural" and any(r["category"] == "自然持续出现词" and r["relevance"]["status"] == "review" for r in results):
            core_status[key] = "暂无可进入投放清单的关键词，相关性待核查"
        else:
            core_status[key] = "本期无符合条件的关键词"
    return {
        "title": "跨境吴老师自然排名与广告展示诊断", "schema_version": 2,
        "status": "partial" if overall_partial else "completed",
        "request": {"asin": asin, "country": country, "days": n},
        "source": source, "window": window, "window_complete": window_complete,
        "product_available": has_product,
        "product_gap": "" if has_product else "商品信息或其来源不可得；相关性待核查，保留排名分类和广告独有核查",
        "source_gaps": source_gaps,
        "daily_coverage": [{
            "date": d["date"], "complete": d["ready"],
            "raw_rows": d["raw_rows"], "unique_rows": len(d["entries"]),
            "keyword_count": len({e["keyword"] for e in d["entries"]}),
            "organic_observed_keywords": daily_counts[d["date"]]["organic"],
            "ad_observed_keywords": daily_counts[d["date"]]["ad"],
            "gaps": d["gaps"], "pagination": d["pagination"],
            "completion_evidence": d["completion_evidence"], "date_evidence": d["date_evidence"],
        } for d in ordered_days],
        "keyword_count": len(keywords), "category_counts": dict(counts),
        "keywords": results, "core_natural": core_natural, "core_ad_only": core_ad,
        "core_status": core_status,
        "limitations": [
            "未观测到仅限数据源采集范围，不等于未投放或全天无展示",
            "持续出现指每日观测到，不代表排名稳定或广告有效",
            "脚本不核验源证据真实性；相关性属于需证据支持的初步判断",
            "未核验实际广告状态、完整历史 Listing、竞价、预算及广告收益",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="跨境吴老师日级关键词覆盖统计（离线，不调用广告接口）")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        with args.input.open(encoding="utf-8-sig") as handle:
            result = analyze(json.load(handle))
        with args.output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        print(f"分析未完成：{exc}", file=sys.stderr)
        return 2
    print(f"跨境吴老师日级统计已完成：{result['status']}，关键词 {result['keyword_count']} 个。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
