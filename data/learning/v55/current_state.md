# V5.5 Current Learning State

Updated: 2026-09-06 KST

## 운영 원칙
- 30분 복기 결과를 이 GitHub 공유 학습 저장소에 누적한다.
- 기존 성공군 16개와 신규 성공/실패/진행중 사이클을 함께 비교한다.
- 과거 원자료가 없는 값은 N/A이며 사후 가격으로 과거 stage를 역으로 만들어내지 않는다.
- 새로운 반복패턴은 기존 패턴에 강제로 맞추지 않고 별도 후보로 축적한다.
- 혼동행렬과 효과 검증 전에는 V5.5 임계값 및 realtime_radar_v55.py를 자동 변경하지 않는다.

## 현재 확인된 데이터 상태
- `data/live/radar_events/20260906/v55_events.jsonl` 게시가 확인됐다.
- 현재 확보 범위는 2026-09-06 05:26:28~10:20:03 KST, 36,326개 이벤트다.
- 이 범위부터 market+cycle_id별 stage1~6/reset/drop/reserve/t_handover 및 +3/-4틱 선후관계를 원본 그대로 판정한다.
- 게시 시작 이전 구간이나 이벤트에 없는 가격경로는 역추정하지 않고 N/A를 유지한다.
- 완전 회차는 08:40 KST, 미세구조는 08:57 KST로 이벤트 원본보다 느리므로 구조값의 시각을 분리 표기한다.

## 최근 복기에서 보존할 가격경로 사례
- ARB: +25%, +30% 이상으로 확장된 강한 성공 가격경로. 정확한 V5.5 cycle_id/event sequence는 이벤트 원본 부재로 N/A.
- OP: 초기 거래대금 이상 이후 ASK 우위에서 가격방어/재축적, 이후 BID 우위로 전환하며 +10% 이상 확장한 성공 가격경로 후보. 정확한 stage sequence는 N/A.
- MOODENG: 거래 증가 후 +10% 이상 순간돌파 뒤 이탈한 실패/가짜발사 가격경로 후보. 정확한 stage sequence는 N/A.
- RAY: +10% 돌파 후 결과 관찰 진행중 사례.
- AERO: 최소폭 +10% 돌파 후 이탈한 가짜발사 후보, 결과 관찰 진행중.

## 2026-09-06 10:20 KST 공식 레이더 상태
- 누적 퍼널: Stage1 914, Stage2 615, Stage4 89, Stage5 77, Stage6 21.
- 현재 살아 있는 Stage5/6 cycle은 0개다. 따라서 공식 최종매수 확정은 없음.
- 진행중 Stage3 선행 후보:
  - PLUME cycle 2: reserve Stage3가 10:07 handover, 10:18 가격 19.2원(T+0.52%), drop 축 0. 08:57 스냅샷은 anomaly 4.65배·2,629만원 전액 BID·51건·Higher Low.
  - GAS cycle 4: Stage2 BID 219만원/ASK 0, 5건, 현재 1,690원(T+0.12%), drop 축 0. Higher Low·slope15 +0.20·MA120 gap -0.47%이나 예상 D-day와 실제 교차는 아직 없음.
  - HBAR cycle 2: 현재 111원(T+1.83%), drop 축 0. 08:57 스냅샷은 anomaly 11.88배·2.30억원 BID·54건, MA120 예상 D-8.97이나 실제 교차·D-day 단축은 없음.
- 공식 실패 갱신: FLOCK cycle 19는 반복 Stage5 후 Stage6 없이 reset, EDGE cycle 15는 20초간 가격/T 이탈과 ASK 우위로 drop/reset.
- 공식 진행중: IOST cycle 2는 10:15 Stage5 후 5초 무체결로 Stage3 복귀.
- 신규 성공/MISSED 확정은 없음.

## 새 반복패턴/보존
- reserve Stage3가 검증된 채 handover된 PLUME·CVC는 오래된 T를 버리지 않고 가격방어/재축적을 이어가는 보존 사례다.
- Stage3의 `drop_axes bad_count=0`과 가격 T 근접만으로 매수 확정하지 않고, 분산된 BID·거래대금 anomaly·Higher Low/MA 수렴 중 하나가 합류할 때 선행 후보로 승격한다.
- Stage2가 1건 소액이어도 이후 15분 분산 BID가 실제로 커지는 HBAR형은 초기 체결수만으로 영구 제외하지 않는다. 반대로 후속 분산 팽창이 없으면 제외한다.
- 골든크로스는 실제 교차, MA15→MA50/120 gap, gap 변화, D-day 출현·단축·소멸을 각각 분리 기록한다.
- 위 내용은 판정/학습 업데이트이며 임계값·코드 변경 제안은 아니다.

## 수정 제안 상태
- Stage 6 확인 후보(2026-09-06 재현): `Stage 5 +3틱 도달 후 3초 동안 +2틱 이상 유지`, 실패 시 영구탈락하지 않고 Stage 3 복귀.
  - 첫 시도만 자르면 성공군 15개 중 9개 유지/6개 손실이라 단독 즉사조건으로는 부적합.
  - Stage 3 복귀와 재시도를 포함한 전체 재현에서는 성공군 15/15가 다시 Stage 6 확인에 도달.
  - 오늘 확보된 FLOCK/ZRO 5개 실패 사이클 중 FLOCK 3개는 첫 확인에서 제거, ZRO 2개는 통과(실패 제거율 60%).
  - 최근 10초 BID>ASK/순매수(+)를 Stage 6에서 다시 확인하는 조건은 라이브 실패 5/5가 통과하고 성공군 첫 시도 5개를 손실하여 제거력이 없음.
  - 성공군 확인 지연은 대부분 수초~수분이나 CHIP은 약 35,801초, MOC 약 1,160초, SNT 약 723초가 발생하므로 수익/진입지연 검증이 추가로 필요.
- 동일 종목 중복 신호: 포지션 보유 또는 동일 발사 파동 중 재발한 Stage 6는 신규 매수신호와 분리하는 dedupe/position lock 후보. 이는 실패 판별조건이 아니라 중복주문 방지장치로 별도 검증.
- 거래조건/임계값 확정 변경: 없음. 위 후보는 ZRO를 제거하지 못했고 진입지연 검증이 남아 있음.
- 데이터 파이프라인: 서버 `v55_events.jsonl`을 GitHub의 `data/live/radar_events/YYYYMMDD/`에 게시할 필요가 있음.
- 코드 자동 수정/배포: 금지 유지.

## 재현 근거 파일
- `evidence/stage6_confirmation_first_attempt_20260906.csv`
- `evidence/stage6_confirmation_first_attempt_summary_20260906.csv`
- `evidence/stage6_confirmation_reentry_success15_20260906.csv`
