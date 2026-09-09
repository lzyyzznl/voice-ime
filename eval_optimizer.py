#!/usr/bin/python3.6
"""voice-ime 优化助手评估脚本

用当前 config.json 的 llm 提示词（可临时覆盖）跑评估集，逐条调 DeepSeek，
按"归一化匹配"判定 PASS/FAIL，输出汇总报告。调整 system_prompt/prompt 后
运行本脚本对比 PASS 率。

用法:
    /usr/bin/python3.6 eval_optimizer.py                      # 用 config.json 当前提示词
    /usr/bin/python3.6 eval_optimizer.py --limit 5            # 只跑前 5 条
    /usr/bin/python3.6 eval_optimizer.py --show-raw           # 显示模型原始输出（含标签）
    /usr/bin/python3.6 eval_optimizer.py \
        --system-prompt "自定义 system 提示词" --prompt "原文："   # 不改文件试提示词

判定规则（三层递进，避免 1:1 字符对比误判合理改写）:
    1) exact   归一化相等（去空白/标点、NFKC、小写）
    2) subseq  双向字符子序列匹配（容忍加词/删词/语序内插词）
    3) judge   LLM 语义判定（容忍同义改写/语序调整/标点差异，
               不允许改变实质内容或遗漏关键信息）
    辅助信号（不参与判定，仅报告）：口头语残留、空输出异常。
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request

DEFAULT_EVAL = "eval_set.json"
DEFAULT_CFG = "config.json"
# 口头语残留检测词（输出中仍含这些词 → 报告标记，不直接判 FAIL）
HALLU_WORDS = ["嗯", "啊", "那个", "就是", "然后", "哎呀", "嗯嗯", "好的"]


def load_cfg(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f).get("llm") or {}
    return cfg


def load_eval(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def normalize(s):
    """归一化：NFKC + 去空白/标点 + 小写。中文保留，仅比较实质内容"""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s\W_]+", "", s).lower()


def has_hallucination(s):
    return [w for w in HALLU_WORDS if w in (s or "")]


def is_subseq(a, b):
    """a 是否为 b 的字符子序列（按序删除若干字符可得 a）；容忍加词/删词/语序内的插词"""
    it = iter(b)
    return all(c in it for c in a)


def llm_judge(cfg, expected, output):
    """LLM 语义判定：输出与期望是否语义等价（允许同义改写/语序调整/标点差异）。
    返回 (ok, answer)"""
    endpoint = cfg.get("endpoint", "").strip()
    key_var = cfg.get("api_key", "").strip() or "DEEPSEEK_API_KEY"
    api_key = os.environ.get(key_var, "").strip()
    model = cfg.get("model", "").strip()
    messages = [
        {"role": "system",
         "content": "你是文本评估助手，只回答 yes 或 no。"},
        {"role": "user",
         "content": "以下是润色任务的期望输出和实际输出。判断实际输出是否在语义上等同于期望："
                    "允许同义改写、语序调整、标点差异；不允许改变实质内容或遗漏关键信息。\n"
                    "期望: %s\n输出: %s" % (expected, output)},
    ]
    body = {
        "model": model,
        "messages": messages,
        "thinking": {"type": "disabled"},
        "temperature": 0,
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    answer = data["choices"][0]["message"]["content"].strip().lower()
    return answer.startswith("yes"), answer


def call_llm(cfg, text):
    """与草稿窗口同款请求：system_prompt + prompt(原文:) + 非思考 + <output> 标签提取"""
    endpoint = cfg.get("endpoint", "").strip()
    if not endpoint:
        raise RuntimeError("config.json 未配置 llm.endpoint")
    key_var = cfg.get("api_key", "").strip() or "DEEPSEEK_API_KEY"
    api_key = os.environ.get(key_var, "").strip()
    if not api_key:
        raise RuntimeError("未找到 API Key：环境变量 %s 未设置" % key_var)
    model = cfg.get("model", "").strip()
    system_prompt = cfg.get("system_prompt", "").strip()
    prompt = cfg.get("prompt", "").strip()

    messages = [{"role": "user",
                 "content": (prompt + "\n" + text) if prompt else text}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    body = {
        "model": model,
        "messages": messages,
        "thinking": {"type": "disabled"},
        "temperature": 0,   # 固定最低随机性：评估可复现，结果稳定
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = data["choices"][0]["message"]["content"].strip()
    m = re.search(r"<output>(.*?)</output>", raw, re.S)
    return (m.group(1).strip() if m else raw), raw


def main():
    ap = argparse.ArgumentParser(description="voice-ime 优化助手评估")
    ap.add_argument("--eval-set", default=DEFAULT_EVAL)
    ap.add_argument("--config", default=DEFAULT_CFG)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
    ap.add_argument("--show-raw", action="store_true", help="显示模型原始输出（含标签）")
    ap.add_argument("--system-prompt", default=None, help="临时覆盖 system_prompt")
    ap.add_argument("--prompt", default=None, help="临时覆盖 prompt（user 前缀）")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    if args.system_prompt is not None:
        cfg["system_prompt"] = args.system_prompt
    if args.prompt is not None:
        cfg["prompt"] = args.prompt
    items = load_eval(args.eval_set)
    if args.limit > 0:
        items = items[:args.limit]

    print("评估集: %s (%d 条) | 模型: %s | system_prompt: %s"
          % (args.eval_set, len(items), cfg.get("model", "?"),
             "自定义" if args.system_prompt is not None else "config"))
    print("=" * 72)

    passed = 0
    times = []
    fails = []
    hallu_samples = 0
    empty_abnormal = 0
    level_count = {"exact": 0, "subseq": 0, "judge_pass": 0, "judge_fail": 0}

    for i, item in enumerate(items, 1):
        text = item["input"]
        expected = item.get("expected", "")
        t0 = time.monotonic()
        try:
            output, raw = call_llm(cfg, text)
            err = None
        except Exception as e:
            output, raw = "", ""
            err = str(e)
        dt = time.monotonic() - t0
        times.append(dt)

        # 三层判定：归一化相等 → 双向子序列（容忍加词/删词/标点）→ LLM 语义判定
        judge_ans = ""
        if err:
            ok, level = False, "error"
        else:
            na, ne = normalize(output), normalize(expected)
            if na == ne:
                ok, level = True, "exact"
            elif (not na) and ne:
                # 输出为空但期望非空：空串是任何串的子序列，不能走 subseq 放行 → 语义判定
                judge_t0 = time.monotonic()
                try:
                    judge_ok, judge_ans = llm_judge(cfg, expected, output)
                except Exception as e:
                    judge_ok, judge_ans = False, "judge 异常: %s" % e
                times.append(time.monotonic() - judge_t0)
                ok, level = judge_ok, ("judge_pass" if judge_ok else "judge_fail")
            elif is_subseq(ne, na) or is_subseq(na, ne):
                ok, level = True, "subseq"
            else:
                judge_t0 = time.monotonic()
                try:
                    judge_ok, judge_ans = llm_judge(cfg, expected, output)
                except Exception as e:
                    judge_ok, judge_ans = False, "judge 异常: %s" % e
                times.append(time.monotonic() - judge_t0)
                ok, level = judge_ok, ("judge_pass" if judge_ok else "judge_fail")
        level_count[level] = level_count.get(level, 0) + 1

        if ok:
            passed += 1
            verdict = "PASS"
        else:
            verdict = "FAIL"
            fails.append((i, item.get("scenario", ""), text, expected, output, err, level))

        hallu = has_hallucination(output) if not err else []
        if hallu:
            hallu_samples += 1
        empty_bad = (not err) and (not output.strip()) and expected.strip()
        if empty_bad:
            empty_abnormal += 1

        print("[%s] %02d/%02d 场景=%s 判定=%s 耗时=%.2fs"
              % (verdict, i, len(items), item.get("scenario", ""), level, dt))
        print("  输入: %s" % text)
        print("  期望: %s" % expected)
        print("  输出: %s" % (output if output else "(空)"))
        if err:
            print("  错误: %s" % err)
        elif level.startswith("judge"):
            print("  judge: %s" % judge_ans)
        elif args.show_raw and raw != output:
            print("  原始: %s" % raw)
        if hallu:
            print("  注意: 口头语残留 %s" % hallu)
        if empty_bad:
            print("  注意: 期望非空但输出为空")

    total = len(items)
    rate = passed * 100.0 / total if total else 0
    avg = sum(times) / len(times) if times else 0
    print("=" * 72)
    print("汇总: PASS %d/%d (%.1f%%) | 平均耗时 %.2fs | 口头语残留样本 %d | 空输出异常 %d"
          % (passed, total, rate, avg, hallu_samples, empty_abnormal))
    print("判定分布: 精确匹配 %d | 子序列匹配 %d | 语义判定通过 %d | 语义判定失败 %d | 错误 %d"
          % (level_count.get("exact", 0), level_count.get("subseq", 0),
             level_count.get("judge_pass", 0), level_count.get("judge_fail", 0),
             level_count.get("error", 0)))
    if fails:
        print("失败样本:")
        for i, sc, text, expected, output, err, level in fails:
            print("  #%d %s | 判定=%s | 输入=%s | 期望=%s | 输出=%s | 错误=%s"
                  % (i, sc, level, text, expected, output or "(空)", err or "-"))
    # 落盘（追加）：logs/eval-report.log，便于多次调提示词后对比
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "logs", "eval-report.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("%s | PASS %d/%d (%.1f%%) | 平均耗时 %.2fs | 判定(exact/subseq/judge) %d/%d/%d | 口头语残留 %d | 空输出异常 %d\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), passed, total, rate,
                       avg, level_count.get("exact", 0), level_count.get("subseq", 0),
                       level_count.get("judge_pass", 0) + level_count.get("judge_fail", 0),
                       hallu_samples, empty_abnormal))
            for i, sc, text, expected, output, err, level in fails:
                f.write("    FAIL #%d %s | 判定=%s | 输入=%s | 期望=%s | 输出=%s | 错误=%s\n"
                        % (i, sc, level, text, expected, output or "(空)", err or "-"))
    except Exception as e:
        print("报告落盘失败: %s" % e)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
