"""Rules-only vs rules+model on the same held-out SGD calls. Needs ANTHROPIC_API_KEY, or OPENAI_API_KEY / GROQ_API_KEY with --provider openai / groq.

  python -m eval.compare_readers /path/to/dstc8-schema-guided-dialogue/train [--n 400] [--provider anthropic] [--model claude-haiku-5-5]

Model answers are cached in eval/.llm_cache.json, so re-runs are free and repeatable.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run(sgd, n, out, port, hl_port, extra_env):
    env = {**os.environ, **extra_env}
    cmd = [sys.executable, '-m', 'eval.sgd_replay', sgd, '--n', str(n), '--out', out, '--port', str(port), '--hl-port', str(hl_port)]
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print(p.stdout.strip())
    if p.returncode:
        print(p.stderr[-2000:])
        raise SystemExit(p.returncode)
    return json.load(open(out))['summary']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sgd_dir')
    ap.add_argument('--n', type=int, default=400)
    ap.add_argument('--provider', default='anthropic', choices=['anthropic', 'openai', 'groq'])
    ap.add_argument('--model', default=None)
    a = ap.parse_args()
    key = {'anthropic': 'ANTHROPIC_API_KEY', 'openai': 'OPENAI_API_KEY', 'groq': 'GROQ_API_KEY'}[a.provider]
    if not os.environ.get(key):
        raise SystemExit(f'Set {key} first.')
    cache = str(Path('eval/.llm_cache.json').resolve())
    model_env = {'VAADA_EXTRACTOR': a.provider, 'VAADA_EXTRACT_MODE': 'vote', 'VAADA_LLM_CACHE': cache}
    if a.model:
        model_env[{'anthropic': 'VAADA_ANTHROPIC_MODEL', 'openai': 'VAADA_OPENAI_MODEL', 'groq': 'VAADA_GROQ_MODEL'}[a.provider]] = a.model
    print('== rules only')
    base = run(a.sgd_dir, a.n, 'eval/compare_rules.json', 8781, 8791, {'VAADA_EXTRACTOR': 'rules'})
    print('\n== rules + model, voting')
    vote = run(a.sgd_dir, a.n, 'eval/compare_vote.json', 8782, 8792, model_env)

    def pct(x):
        return f'{x[0]}/{x[1]} ({x[0] / max(1, x[1]):.0%})'
    rows = [('Right verdict', f"{base['verdict_accuracy']:.1%}", f"{vote['verdict_accuracy']:.1%}"),
            ('Flags that are correct', f"{base['precision']:.1%}", f"{vote['precision']:.1%}"),
            ('Errors caught', pct(base['faults_caught']), pct(vote['faults_caught'])),
            ('False alarms', pct(base['false_alarms']), pct(vote['false_alarms'])),
            ('Correct bookings accepted', pct(base['accepted_correct']), pct(vote['accepted_correct'])),
            ('Date+time read exactly', pct(base['extraction_exact']), pct(vote['extraction_exact'])),
            ('Sent to review (one reader)', '-', str(vote['single_reader'])),
            ('Readers disagreed (abstained)', '-', str(vote['disagree'])),
            ('No model answer (fell back to rules)', '-', str(vote.get('model_unavailable', '?')))]
    print(f"\n{'':32s}{'rules only':>18s}{'rules + model':>18s}")
    for name, b, v in rows:
        print(f'{name:32s}{b:>18s}{v:>18s}')
    try:
        c = json.load(open(cache))
        tin = sum((e.get('usage') or {}).get('input_tokens', (e.get('usage') or {}).get('prompt_tokens', 0)) for e in c.values())
        tout = sum((e.get('usage') or {}).get('output_tokens', (e.get('usage') or {}).get('completion_tokens', 0)) for e in c.values())
        print(f'\nModel calls cached: {len(c)} · tokens in {tin:,} · out {tout:,} (multiply by your model price per million tokens for cost)')
    except (OSError, ValueError):
        pass


if __name__ == '__main__':
    main()
