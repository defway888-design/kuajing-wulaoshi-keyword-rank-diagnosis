"""Render the versioned, offline dashboard; no network, MCP, or advertising writes."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

from analyze import analyze, CATEGORIES, position_semantics_known


PACKAGE = Path(__file__).resolve().parents[1]
TEMPLATE_ID = "keyword-rank-progressive-v1"
IDS = ("natural", "natural_watch", "ad", "ad_watch", "both", "alternate", "neither", "insufficient")
CATEGORY_IDS = dict(zip(CATEGORIES, IDS))


def read_template(package=PACKAGE):
    manifest = json.loads((package / "assets" / "template_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("template_id") != TEMPLATE_ID or manifest.get("version") != "1.0.0":
        raise ValueError("模板版本不匹配；请恢复完整的正式 Skill 包，不得重新绘制替代页面")
    content = (package / "assets" / "dashboard_template.html").read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest.get("sha256"):
        raise ValueError("固定模板 SHA256 不匹配；停止生成，请恢复原始模板")
    template = content.decode("utf-8")
    if template.count("__REPORT_DATA__") != 1:
        raise ValueError("固定模板数据槽异常")
    return template


def view_data(payload, preview=False):
    # Recompute from the normalized source input, never trust supplied totals/classifications.
    result = analyze(payload)
    source = result["source"]
    keys = []
    for item in result["keywords"]:
        facts = []
        for day in item["daily"]:
            # Raw responses may contain account information: allowlist presentation fields.
            records = [{"kind": p["kind"], "valid": position_semantics_known(p, source),
                        "sourceValid": p.get("valid", False), "type": p.get("source_type", ""),
                        "totalRank": p.get("total_rank"), "pageRank": p.get("page_rank"),
                        "page": p.get("page"), "capturedAt": p.get("captured_at")}
                       for p in day["positions"]]
            facts.append({**{k: day[k] for k in ("date", "organic", "ad", "ambiguous")}, "records": records})
        keys.append({"keyword": item["keyword"], "zh": "", "category": CATEGORY_IDS[item["category"]],
                     "o": item["organic_days"], "a": item["ad_days"], "b": item["both_days"],
                     "relevance": item["relevance"]["status"], "reason": item["relevance"]["reason"],
                     "relevanceEvidence": item["relevance"]["evidence"], "gaps": item["gaps"],
                     "directions": item["directions"], "daily": facts,
                     "coreA": item["keyword"] in result["core_natural"],
                     "coreB": item["keyword"] in result["core_ad_only"]})
    coverage = []
    for day in result["daily_coverage"]:
        paging = day["pagination"]
        coverage.append({**{k: day[k] for k in ("date", "complete", "raw_rows", "unique_rows", "keyword_count",
                         "organic_observed_keywords", "ad_observed_keywords", "gaps", "completion_evidence", "date_evidence")},
                         "pagination": {k: paging.get(k) for k in ("complete", "evidence", "requested", "received", "reported_total", "received_rows")}})
    return {"mode": "synthetic-preview" if preview else "xiyou-diagnosis", "templateId": TEMPLATE_ID,
            **result["request"], "dates": result["window"]["dates"],
            "window": {k: result["window"].get(k) for k in ("initial_end", "end", "shift_days", "shift_reason", "shift_evidence", "observation_label")},
            "status": result["status"], "windowComplete": result["window_complete"],
            "coreStatus": result["core_status"], "categoryCounts": result["category_counts"],
            "timezone": source.get("timezone") or "未知", "organicScope": source.get("coverage", {}).get("organic") or "未确认",
            "adScope": source.get("coverage", {}).get("ad") or "未确认", "sourceNote": "西柚 MCP 日级关键词数据",
            "sourceGaps": result["source_gaps"], "dateEvidence": source.get("date_basis_evidence", ""),
            "mappingEvidence": source.get("mapping_evidence", ""),
            "productTitle": payload.get("product", {}).get("title") or "未取得可核验商品标题",
            "productEvidence": payload.get("product", {}).get("evidence", ""), "productGap": result["product_gap"],
            "coverage": coverage, "keywords": keys, "limitations": result["limitations"]}


def render(payload, preview=False, package=PACKAGE):
    template = read_template(package)
    data = view_data(payload, preview)
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for char, escaped in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        encoded = encoded.replace(char, escaped)
    return template.replace("__REPORT_DATA__", encoded), data


def main(argv=None):
    parser = argparse.ArgumentParser(description="跨境吴老师固定模板离线看板渲染器")
    parser.add_argument("--input", type=Path, required=True, help="本次规范化源输入，不是 analyze 输出")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preview", action="store_true", help="仅维护测试：醒目标记合成预览；不绕过任何接入记录或分析校验")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
        html, data = render(payload, args.preview)
        with args.output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(html)
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        print(f"看板未生成：{exc}", file=sys.stderr)
        return 2
    print(f"跨境吴老师离线看板已生成：{data['status']}，{len(data['keywords'])} 个关键词。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
