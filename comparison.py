"""Read-only production comparison. Processed URLs are NOT delivery receipts."""
import base64
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import requests


def hashes(urls):
    return {hashlib.sha256(url.encode()).hexdigest() for url in urls}


def record(state, production, now, mode, counts):
    audit = state.setdefault('comparison', {})
    current = hashes(production.get('sent', []))
    test = hashes(a['link'] for s in state.get('stories', {}).values() for a in s['articles'])
    baseline = 'production_baseline' not in audit
    if baseline:
        audit['started_at'] = now
        audit['production_baseline'] = sorted(current)
    observed = set(audit.get('production_observed', [])) | (current - set(audit['production_baseline']))
    audit['production_observed'] = sorted(observed)
    pending = hashes(a['link'] for a in production.get('pending', []))
    missing = observed - test
    audit['unmatched_hashes'] = sorted(missing)
    row = {'at': now, 'mode': mode, 'baseline_only': baseline,
           'production_observed_since_start': len(observed),
           'matched_test_urls': len(observed & test),
           'unmatched_urls_for_review': len(missing),
           'production_pending_urls': len(pending),
           'test_total_articles': sum(len(s['articles']) for s in state.get('stories', {}).values()),
           'test_total_stories': len(state.get('stories', {})),
           'test_waiting_stories': sum(not s.get('message_id') for s in state.get('stories', {}).values()),
           **counts}
    audit.setdefault('runs', []).append(row)
    audit['runs'] = audit['runs'][-1100:]  # About two weeks at the configured cadence.
    return row


def collect(state, repository, headers, mode, counts):
    now = datetime.now().astimezone().isoformat()
    try:
        response = requests.get(f'https://api.github.com/repos/{repository}/contents/state.json',
                                headers=headers, params={'ref': 'main'}, timeout=20)
        response.raise_for_status()
        production = json.loads(base64.b64decode(response.json()['content']))
        row = record(state, production, now, mode, counts)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        row = {'at': now, 'comparison_unavailable': True, **counts}
        state.setdefault('comparison', {})['last_error_at'] = now
    print('COMPARISON ' + json.dumps(row, ensure_ascii=False))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        lines = ['## 뉴스봇 비교 기록', '',
                 '기존 봇의 처리 기록에는 발송·대기·제외가 섞여 있습니다. 미일치 URL은 누락 확정이 아닌 검토 대상입니다.', '',
                 '첫 실행은 비교 기준점이며 이후 관측된 URL부터 대조합니다. URL이 다른 재전송 기사는 별개로 계산합니다.', '',
                 '| 항목 | 값 |', '|---|---|']
        labels = {'test_send_success': '이번 실행 새 메시지 전송 성공',
                  'test_edit_success': '이번 실행 기존 메시지 수정 성공(동일 내용 포함)',
                  'production_observed_since_start': '비교 시작 후 기존 봇 신규 처리 URL',
                  'matched_test_urls': '그중 테스트봇에도 수집된 URL',
                  'unmatched_urls_for_review': '테스트봇 미일치 URL · 검토 필요',
                  'test_total_articles': '테스트봇 누적 기사',
                  'test_total_stories': '테스트봇 누적 이슈',
                  'test_waiting_stories': '테스트봇 발송 대기 이슈',
                  'baseline_only': '이번 실행은 기준점 설정',
                  'comparison_unavailable': '기존 봇 기록 조회 실패'}
        lines += [f'| {labels[k]} | {v} |' for k, v in row.items() if k in labels]
        Path(summary).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return row
