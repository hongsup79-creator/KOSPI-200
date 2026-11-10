# 코스피200 정배열 대시보드

매일 저녁 코스피200 구성종목 중 이동평균선 정배열 종목을 자동으로 뽑아 웹페이지로 보여줍니다.
서버 없이 GitHub만으로 무료 운영됩니다.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `index.html` | 화면 (표·정렬·기준일 선택) |
| `scripts/update_kospi200.py` | 시세 수집 → 이동평균 계산 → JSON 저장 |
| `.github/workflows/update.yml` | 평일 20:00(KST)마다 위 스크립트 자동 실행 |
| `requirements.txt` | 필요한 파이썬 패키지 목록 |
| `data/kospi200/` | 결과 데이터 (처음엔 데모 데이터가 들어 있음) |

## 올리는 순서 (PC 웹브라우저 기준, 약 10분)

1. **GitHub 가입·로그인** → 오른쪽 위 `+` → `New repository`
   - 이름 예: `kospi200` / **Public** 선택 / `Create repository`
2. 만들어진 저장소 화면에서 `uploading an existing file` 클릭
   → 압축을 푼 폴더 안의 **내용물 전체**(index.html, scripts, data, .github 등)를 끌어다 놓기 → `Commit changes`
   - `.github` 폴더는 맥에서 숨김 폴더라 안 보일 수 있습니다. Finder에서 `Cmd + Shift + .` 을 누르면 보입니다.
   - 업로드 후 저장소에 `.github/workflows/update.yml`이 있는지 꼭 확인하세요. 이게 없으면 자동 갱신이 안 됩니다.
3. **Settings → Pages**
   - Source: `Deploy from a branch`, Branch: `main` / `/(root)` → `Save`
   - 1~2분 뒤 `https://아이디.github.io/kospi200/` 주소가 생깁니다. (이때는 데모 데이터 화면)
4. **Settings → Actions → General** → 맨 아래 Workflow permissions → `Read and write permissions` → `Save`
5. **KRX 계정 등록** (시세 수집 라이브러리 pykrx가 KRX 로그인을 요구합니다)
   - [KRX 정보데이터시스템](https://data.krx.co.kr)에서 무료 회원가입
   - 저장소 **Settings → Secrets and variables → Actions → New repository secret** 에서 두 개 등록
     - Name `KRX_ID` / Secret: KRX 아이디
     - Name `KRX_PW` / Secret: KRX 비밀번호
   - Secret은 암호화되어 저장되고 로그에도 `***`로 가려져 공개 저장소여도 노출되지 않습니다.
6. **Actions 탭** → `코스피200 데이터 갱신` → `Run workflow` 버튼
   - 5~10분 걸립니다. 초록 체크가 뜨면 성공, 주소를 새로고침하면 실제 데이터로 바뀌고 노란 '데모' 안내가 사라집니다.
   - 이후로는 평일 저녁 8시에 알아서 갱신됩니다.

## 에러가 났을 때

Actions 탭에서 빨간 X 표시된 실행 → `update` → 빨간 줄 로그를 복사해서 Claude에게 붙여넣고 "고쳐줘"라고 하면 됩니다.
무료 시세 소스(KRX)는 정책 변경으로 가끔 막히는데, 이 경우도 같은 방법으로 대체 소스로 바꿀 수 있습니다.

## 기준 바꾸기

`scripts/update_kospi200.py` 맨 위 `SECTIONS` 부분만 고치면 됩니다.

```python
SECTIONS = [
    {"id": "short", "title": "단기 정배열", "mas": [5, 10, 20]},
    {"id": "mid", "title": "중기 정배열", "mas": [20, 60, 120]},
]
```

- '완전정배열' 배지 = 종가 > 5 > 10 > 20 > 60 > 120일선이 모두 정렬된 종목
- 연속일수 = 해당 정배열 상태가 오늘까지 며칠 연속 유지됐는지 (거래일 기준)
- 구성종목은 최신 코스피200 기준으로 과거 날짜에도 동일하게 적용합니다.
- '%R 눌림' 배지 = 윌리엄스 %R(14일)이 −80 이하인 종목. 관찰용이며 기존 규칙·등급에는 쓰지 않습니다.
  신호 성적표 ⑥에서 '중기 정배열 전체' vs '중기 정배열 + %R −80 이하 진입'의 이후 성과를 첫 기록일부터 비교합니다(`data/forward/*_wr.json`).

## 🛢️ 유가 탭 (관찰용)

'유가는 코스피를 선행한다'는 주장을 데이터로 확인하는 탭입니다. 매매 규칙·등급에는 쓰지 않습니다.

- 코스피(네이버)와 브렌트·WTI(야후 `BZ=F`, `CL=F`) 주간 종가를 겹쳐 보고, 슬라이더로 유가를 0~6개월 뒤로 밀어 볼 수 있습니다.
- 월간 수익률 상관을 시차 −6~+6개월로 계산합니다(+k = 유가가 k개월 앞섬). 기준선 ±2/√n을 넘어야 의미 있는 관계로 봅니다.
- 36개월 이동 상관으로 관계가 시기마다 바뀌는지, 유가가 3개월 ±10% 움직인 뒤 코스피 1·3개월 성적을 평소와 비교합니다.
- 통계는 브렌트 기준입니다(WTI는 2020년 4월 마이너스 가격 때문에 수익률이 왜곡됨). 결과는 `data/oil.json`.

## 참고

- 섹터는 FinanceDataReader의 KRX 업종 분류를 씁니다(WICS와 명칭이 다를 수 있음).
- 투자 판단용 참고 자료이며 데이터 오류 가능성이 있습니다.
