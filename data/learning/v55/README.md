# Upbit Radar V5.5 Learning Store

이 디렉터리는 V5.5의 30분 복기/학습 결과를 GitHub에 누적하여 다른 ChatGPT 대화와 분석 세션에서도 동일한 근거를 재사용하기 위한 공유 저장소다.

## 확정 판정 규칙
- 대상: Upbit KRW 전종목. KRW-BTC 및 스테이블 제외. 상위 40개 제한 금지.
- Stage 1: rolling 60초 거래대금 / 직전 10개 rolling 60초 baseline >= 2.8x.
- Stage 2: 최근 10초 BID>ASK, 순매수 > 0, BID 금액 증가.
- Stage 3: 가격방어/재축적 대기.
- Stage 4: 3초 high +4틱 이상, last 실제 상승률 >=0.40%, 최근 10초 체결 >=7건, last >= T+8틱.
- Stage 5: 최근 10초 BID>ASK 및 순매수 >0.
- Stage 6: Stage 5 기준가 +3틱 도달 시 최종 매수확정.
- Stage 5 이후 -4틱 또는 5초 연속 무체결이면 유효 T 기준 Stage 3 복귀.
- T TTL: Stage 1~4는 240분. 120분부터 reserve T 수집, 만료 후 검증 reserve T로 handover.
- Stage 5 TTL 예외: 최대 12시간.
- 판정키: market + cycle_id.
- 로그/초단위 원자료가 없으면 추정 금지, N/A.

## 학습 원칙
30분마다 stage1~stage6, reset, drop, stage4_reject, stage4_to_stage3, stage5_to_stage3, reserve, t_handover를 market+cycle_id 단위로 누적한다. Stage 4/5/6 이후 최고가·최저가, +3틱 도달시간, -4틱 선도 여부, 최대 무체결 간격, 당일 최대상승률을 추적해 성공/약한상승/가짜발사/진행중으로 분류한다.

새 실패 사이클은 가능한 경우 T-10분부터 결과확정 이후까지 가격, tick_size, 거래대금 배수, BID/ASK, 순매수, BID금액, 체결수, 3초 high/last/rise_pct, T ticks, DROP/복귀/handover 사유를 보존한다.

성공군 16개와 누적 실패군을 대조하며 후보조건은 단독효과 후 조합효과 순서로 평가한다. 성공 유지율, 실패 제거율, 진입 지연, 진입가격 손실과 혼동행렬이 확보된 경우에만 수정안을 제안한다. 분석 결과만으로 realtime_radar_v55.py를 자동 수정하거나 배포하지 않는다.

## 파일
- `learning_log.jsonl`: 30분 회차별 학습 요약을 append 개념으로 누적하는 공유 로그.
- `current_state.md`: 최신 누적 상태/핵심 학습점/보존 대상/수정 제안 상태.
- 실제 서버 이벤트 원본의 목표 위치: `data/live/radar_events/YYYYMMDD/v55_events.jsonl`.

현재 GitHub에서 `data/live/radar_events`가 확인되지 않는 경우 이벤트 기반 stage/cycle_id 통계는 N/A로 유지한다.
