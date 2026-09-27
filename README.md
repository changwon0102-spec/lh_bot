# 서울 청년임대주택 공고 알림봇

LH 청약플러스와 SH 공지·주택모집 게시판을 읽고, 서울 청년 대상 공고를 GPT-4o로 요약해 텔레그램으로 보냅니다. Python 3.12와 GitHub Actions를 기준으로 작성했습니다.

> 먼저 `--dry-run`으로 수집 결과를 확인하세요. 이 모드는 API 키 없이 실행하며 Supabase, OpenAI, Telegram을 호출하지 않습니다. GitHub Actions의 무료 사용 범위와 Supabase 무료 플랜을 활용할 수 있지만 **OpenAI API 사용료는 별도**입니다.

## 파일 구성

| 파일 | 용도 |
|---|---|
| `requirements.txt` | 직접 사용하는 패키지와 고정 버전 |
| `requirements.lock.txt` | 전이 의존성을 포함한 전체 버전 고정; Actions 설치에 사용 |
| `schema.sql` | 테이블, RLS, 서버 권한, 원자적 발송 예약 함수 |
| `main.py` | 설정·크롤러·첨부 파서·요약·알림·DB·실행 진입점 |
| `.github/workflows/scraper.yml` | 매일 KST 09:00 / 18:00 실행 |
| `.env.example` | 로컬 환경 변수 예시 |
| `telegram_setup.py` | 봇 이름과 개인 알림용 Chat ID 확인; 발송·설정 변경 없음 |
| `tests/test_pipeline.py` | 외부 연결 없는 Python 테스트 |
| `tests/check_schema.mjs` | 선택 사항: 로컬 메모리 PostgreSQL SQL 검증 |

## 1. Supabase 초기 설정

1. 사용할 Supabase 프로젝트를 준비합니다.
2. **`schema.sql`은 이 프로젝트 폴더에 있는 로컬 파일입니다. Supabase SQL Editor에 자동으로 나타나지 않습니다.** 다음 순서로 파일의 내용을 복사해 실행합니다.
   - PC의 프로젝트 폴더에서 `schema.sql`을 텍스트 편집기로 엽니다. Codex에서 해당 파일을 열어도 됩니다.
   - 파일 내용 전체를 선택(`Ctrl+A`)하고 복사(`Ctrl+C`)합니다.
   - Supabase 대시보드에서 사용할 프로젝트의 **SQL Editor**를 열고 새 쿼리를 만듭니다.
   - 빈 쿼리 입력란에 복사한 SQL 내용을 붙여넣고(`Ctrl+V`) **Run**을 누릅니다. 파일명이나 파일 경로를 입력하는 것이 아닙니다.
   - 오류 없이 완료되면 **Table Editor**를 열어 `public` 스키마의 `announcements`, `delivery_jobs` 테이블이 생성되었는지 확인합니다. 빈 테이블인 것이 정상입니다.
3. 프로젝트 URL과 **서버용 Secret key (`sb_secret_...`)** 또는 기존 **service_role key**를 준비합니다. Publishable/anon key로는 이 봇의 테이블을 사용할 수 없습니다.
4. Data API에서 `public` 스키마가 노출되어 있어야 합니다.

테이블은 아래 두 개입니다. 컬럼별 설명은 SQL 주석에도 있습니다.

| 테이블 | 주요 컬럼 | 역할 |
|---|---|---|
| `announcements` | `source`, `post_id`, `title`, `url`, `published_at`, `summary`, `telegram_message_id`, `created_at` | 전송에 성공한 공고만 저장 |
| `delivery_jobs` | `source`, `post_id`, `payload`, `status`, `claim_token`, `lease_until`, `attempts`, `summary`, `message`, `last_error` | 실행 중 예약 및 실패·전송 불확실 상태 보관 |

두 테이블의 기본키는 `(source, post_id)`입니다. LH는 `panId`, SH는 `게시판번호:seq`를 사용합니다. RLS를 켜고 `anon`/`authenticated` 접근을 차단했으며 서버 역할에 필요한 테이블·함수 권한만 부여했습니다. 서버 키는 `.env`나 GitHub Secrets에만 저장하세요.

**이 저장소를 만들면서 원격 Supabase 프로젝트를 생성하거나 수정하지 않았습니다.** 아래 초기 설정은 사용할 프로젝트에서 직접 실행해야 합니다.

## 2. 텔레그램 준비

1. Telegram의 **@BotFather**에서 `/newbot`으로 봇을 만들고 토큰을 받습니다.
2. 개인 알림이라면 봇과 대화를 열어 `/start`를 보냅니다. 그룹 알림이라면 봇을 그룹에 추가하고 메시지 발송 권한을 줍니다.
3. 프로젝트의 `.env` 파일에서 `TELEGRAM_BOT_TOKEN=` 오른쪽에 BotFather에게 받은 토큰을 붙여넣고 저장합니다. `TELEGRAM_CHAT_ID`는 다음 단계에서 입력합니다.
4. **개인 알림**이라면 프로젝트 폴더의 **PowerShell**에서 아래 명령을 실행합니다. Python 의존성 설치는 3절을 참고하세요.

```powershell
.\.venv\Scripts\python.exe telegram_setup.py
```

5. 출력된 봇 이름이 `/start`를 보낸 봇인지 확인합니다. `TELEGRAM_CHAT_ID=숫자` 줄을 `.env`의 같은 항목에 넣고 저장합니다. 개인 대화가 여러 개라면 본인의 대화를 선택하세요.

이 스크립트는 Telegram의 `getMe`, `getWebhookInfo`, `getUpdates`를 조회하며 알림을 보내거나 `.env`를 변경하지 않습니다. 대화가 없다고 나오면 연결된 봇에 `/start`를 다시 보내고 실행하세요. 기존 webhook이 있으면 해제하지 않고 종료합니다. 그룹 알림은 해당 그룹에서 발생한 `getUpdates` 응답의 `message.chat.id`를 사용하며, 그룹 ID에는 음수 부호가 포함될 수 있습니다.

## 3. 로컬 설치 및 실행

**Windows PowerShell** — Python 3.12가 설치되어 있다는 기준입니다. 가상환경 활성화나 ExecutionPolicy 변경은 필요 없습니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
Copy-Item .env.example .env
# .env를 편집하여 아래 표의 값을 입력합니다.
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe main.py --dry-run --output-dir artifacts/preview
```

`Copy-Item`은 최초 설정 때만 실행하세요. 이미 설정한 `.env`를 덮어쓰지 마세요.

**Linux/macOS 터미널**:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
.venv/bin/python main.py --dry-run --output-dir artifacts/preview
```

| 환경 변수 / GitHub Secret 이름 | 값 |
|---|---|
| `SUPABASE_URL` | `https://프로젝트참조.supabase.co` |
| `SUPABASE_KEY` | 서버용 Secret key 또는 legacy service_role key |
| `TELEGRAM_BOT_TOKEN` | BotFather에서 받은 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 알림을 받을 채팅 ID |
| `OPENAI_API_KEY` | API 프로젝트 키; ChatGPT 구독과 별도 과금 |
| `OPENAI_MODEL` | 로컬 선택 옵션, 기본 `gpt-4o`; Actions에서는 workflow의 env 수정 |

설정 후 **실제 요약·DB 저장·알림 발송**:

```powershell
.\.venv\Scripts\python.exe main.py
```

특정 사이트만 확인할 수도 있습니다.

```powershell
.\.venv\Scripts\python.exe main.py --dry-run --source LH --output-dir artifacts/lh
.\.venv\Scripts\python.exe main.py --dry-run --source SH --output-dir artifacts/sh
```

`artifacts/`에는 원문 텍스트와 추출 보고서가 저장됩니다. `.env`, `.venv`, `artifacts`, 임시 검증 파일은 Git 추적에서 제외됩니다. 운영 실행은 원본 첨부를 디스크에 보관하지 않고 메모리에서 읽습니다.

## 4. GitHub Actions 설정

1. 코드를 GitHub 저장소의 **기본 브랜치**에 올립니다.
2. **Settings → Secrets and variables → Actions → New repository secret**에서 위 표의 필수 Secret 5개를 등록합니다. `OPENAI_MODEL`은 Secret이 아니며 workflow에 기본값이 있습니다.
3. **Actions → Youth housing alerts → Run workflow**에서 브랜치 `main`, `source=LH`, `dry_run=true`, `max_posts=1`로 먼저 실행합니다. 로그의 `preview`와 `오류=0`을 확인합니다.
4. 이후 같은 설정에서 `dry_run=false`로 실행하면 LH 공고 최대 1건의 요약·알림을 시험할 수 있습니다. 실제 API 사용료가 발생합니다. `source=SH`는 접속·파서 확인용으로 별도 dry-run을 하세요. 정기 실행은 두 사이트를 대상으로 최대 20건을 실제 처리합니다.

```yaml
schedule:
  - cron: '0 0,9 * * *'
```

UTC 00:00은 한국 09:00, UTC 09:00은 한국 18:00입니다. 예약 실행은 기본 브랜치의 workflow를 사용합니다. GitHub 부하로 정각보다 늦어질 수 있고, 비활성 공개 저장소의 스케줄은 중지될 수 있으므로 Actions 실행 이력을 확인하세요. 개인/조직 요금제의 무료 실행량을 초과하면 비용이 발생할 수 있습니다.

중복 실행은 Actions `concurrency`와 DB 예약으로 막습니다. 작업 제한은 30분이고 DB 예약 유효시간은 45분입니다. 실행 제한을 늘린다면 `schema.sql`의 예약 시간도 더 길게 조정하세요.

## 필터와 요약 방식

`main.py` 상단의 리스트를 수정하면 됩니다.

- `TARGET_SITES`: LH 임대주택 게시판, SH 공지사항(`m_241`)·주택모집(`m_247`).
- `TARGET_REGIONS`: 기본 서울·서울특별시.
- `FILTER_KEYWORDS`: 청년매입임대, 청년전세임대, 청년안심주택, 행복주택 등. 공백·구두점을 제거한 뒤 비교합니다.
- `YOUTH_KEYWORDS`, `HOUSING_KEYWORDS`: 제목에 단어가 떨어져 있는 경우를 위한 보완 조건.
- `EXCLUDE_TITLE_KEYWORDS`: 당첨자 발표 등 모집 이후 안내 제외.
- `DETAIL_SELECTORS`: 사이트가 개편되었을 때 본문 선택자 수정 위치.

제목을 먼저 필터링하므로 **제목에 관련 키워드가 전혀 없는 공고는 수집 대상에서 제외**됩니다. LH의 게시판 지역 필드로 후보를 좁히되 “대구광역시 외”, “경기도 외”처럼 지역이 생략된 후보는 LH 공식 서울 지역 검색(`cnpCd=11`) 결과의 공고 ID와 대조합니다. “외”라는 표시만으로 서울을 포함한다고 판단하지 않습니다. 전국·수도권 공고는 후보로 유지합니다. 최종적으로 LLM이 본문·첨부에서 서울 공급/신청 가능 여부와 청년 모집 여부를 확인합니다. 기관 주소나 페이지 하단의 “서울”만으로 판단하지 않습니다. 전국 전세임대처럼 지역 목록이 없는 경우에는 서울에서 신청할 수 있다는 근거가 필요합니다.

`PublicWeb → Crawler → collect_attachments → Summarizer → Telegram → Repository` 순서로 처리합니다.

- PDF: `pdfplumber`, 페이지별 추출 오류 격리. 스캔본은 OCR을 수행하지 않고 누락 경고를 남깁니다.
- HWP 5.x: `olefile`로 `BodyText/Section*`을 읽어 압축과 문단 레코드를 해석합니다. 암호화·배포용·구형 HWP는 경고 후 다음 파일을 계속합니다.
- HWPX: ZIP 내부 `Contents/section*.xml` 텍스트를 안전한 XML 파서로 읽습니다.
- XLSX: 실제 LH 공급주택 목록을 위해 `openpyxl` 지원을 추가했습니다. 저장된 셀 값만 읽으며 수식은 실행하지 않습니다. 저장된 계산 결과가 없는 수식은 빈 값일 수 있습니다.
- `.xls`/`.zip` 별첨, 이미지, 외부 도메인 첨부는 자동 추출 범위 밖입니다. PDF/HWPX의 복잡한 표도 읽는 순서가 달라질 수 있습니다.
- 같은 파일명의 PDF를 온전히 읽은 경우 동일 이름 HWP/HWPX는 중복 형식으로 간주해 생략합니다. PDF에 누락이 있으면 HWP/HWPX도 읽습니다.

GPT-4o Structured Outputs와 Pydantic 검증으로 JSON을 받습니다. 전체 공고의 공급 호수, 구·동 목록, 보증금·월세 범위, 모집 기간을 요약하며 개별 단지 상세 표는 만들지 않습니다. 긴 문서는 **모든 조각을 요약한 뒤 통합**합니다. 파일 단위로 나누고 이어지는 조각에는 문서 첫 부분의 항목명·단위 문맥을 반복합니다. 중간 추출의 부분 금액 범위는 같은 조건끼리 통합하며 총 공급 호수는 중복 합산하지 않습니다. 분할 상한을 넘으면 뒷부분을 버리지 않고 실패로 기록합니다. 이 경우 상한을 조정해 재시도하세요.

금액은 원 단위 정수입니다. 모르는 값은 `null`로 처리하며 `0원`과 구분합니다. 예비입주자 모집 인원과 실제 공급 호수, 대출 지원한도와 보증금, 전환 예시와 기본 월세를 구분하도록 지시합니다. 누락·충돌·불완전한 별첨 때문에 전체 범위가 불확실하면 해당 값은 원문 확인 대상으로 남깁니다. LLM 추출의 사실 정확성은 JSON 검증만으로 보장되지 않으므로 알림 하단 원문을 함께 확인하세요.

서울 이외 지역으로 바꾸려면 지역 리스트뿐 아니라 `SUMMARY_PROMPT`, `seoul_eligible` 판정, SH의 서울 기본값과 메시지 문구도 함께 수정해야 합니다.

## 실행량 조정

로컬 `.env` 또는 workflow `env`에서 설정합니다.

| 변수 | 기본값 | 의미 |
|---|---:|---|
| `LOOKBACK_DAYS` | 30 | 신규 탐색할 공고의 게시일 범위 |
| `MAX_LIST_PAGES` | 5 | 게시판별 최대 목록 페이지 수 |
| `MAX_POSTS_PER_RUN` | 20 | 실행당 실제 처리 건수 상한; 이미 발송한 공고는 제외 |
| `REQUEST_DELAY_SECONDS` | 1.0 | 공식 사이트 요청 사이의 최소 대기 |
| `MAX_ATTACHMENT_MB` | 20 | 첨부 1개 다운로드 제한 |
| `MAX_PDF_PAGES` | 150 | PDF당 파싱 페이지 상한; 초과 시 누락 경고 |
| `LLM_CHUNK_CHARS` | 24000 | LLM 입력 조각의 최대 문자 수; 토큰 수와 다름 |
| `MAX_LLM_CHUNKS` | 12 | 공고당 입력 조각 상한 |

첫 실행은 최근 30일에 게시된 일치 공고를 처리합니다. 이미 모집이 종료된 공고도 포함될 수 있습니다. 이미 DB에 기록된 실패 건은 게시일 범위를 벗어나도 재시도 후보가 됩니다. 수집 페이지·처리 상한에 도달하면 로그에 경고를 남깁니다. 긴 중단 후에는 `LOOKBACK_DAYS`, `MAX_LIST_PAGES`, `MAX_POSTS_PER_RUN`을 늘려 누락을 점검하세요.

## 중복 방지와 장애 복구

1. `announcements`에 같은 `(source, post_id)`가 있으면 상세 다운로드·LLM·발송을 생략합니다.
2. 없으면 `claim_announcement`가 원자적으로 작업을 예약합니다. 이미 다른 실행이 예약한 작업은 생략합니다.
3. 요약까지 완료하면 `begin_delivery`가 예약 소유권과 만료시간을 검사하고 메시지를 저장합니다.
4. Telegram의 `ok=true`와 `message_id`를 확인한 **뒤** `complete_delivery`가 `announcements`를 insert하고 작업을 완료합니다.

| 상태 | 다음 실행의 행동 |
|---|---|
| `preparing` | 45분 내에는 건너뜀, 만료 후 재예약 |
| `failed` | 다음 실행에 재시도; 추출/LLM/설정 오류 또는 명확한 Telegram 거절 |
| `skipped` | 청년 모집이 아니거나 서울 대상 아님; 자동 재시도 안 함 |
| `sending` / `uncertain` | 자동 재발송 안 함; 사람이 실제 수신 여부를 확인 |
| `sent` | 발송 완료; 건너뜀 |

**Telegram과 Supabase를 하나의 트랜잭션으로 묶을 수 없어 엄밀한 exactly-once 전송은 보장할 수 없습니다.** 응답 시간초과나 발송 직후 DB 장애는 메시지가 이미 도착했을 가능성이 있습니다. 이 구현은 그런 건을 자동 재발송하지 않고 보류하므로 중복을 줄이는 대신 수동 확인이 필요합니다. 같은 공고 ID의 본문 수정도 자동 재알림하지 않습니다.

Supabase SQL Editor에서 점검:

```sql
select source, post_id, status, attempts, lease_until, updated_at, last_error
from public.delivery_jobs
where status not in ('sent', 'skipped')
order by updated_at;
```

실행 중인 작업이 없고 Telegram에서 **수신하지 않았음이 확인된 특정 공고**만 아래처럼 재시도 상태로 돌립니다. `실제공고ID`를 바꾸세요. 이전 실행이 살아 있는 동안에는 예약을 초기화하지 마세요.

```sql
update public.delivery_jobs
set status = 'failed', updated_at = now(), last_error = '수동 확인: 미수신, 재시도'
where source = 'LH' and post_id = '실제공고ID'
  and status in ('sending', 'uncertain');
```

이미 메시지를 수신했다면 재발송하지 마세요. 확인한 Telegram `message_id`와 해당 행의 `claim_token`으로 완료 기록만 남깁니다. 아래의 ID/토큰/메시지 번호는 실제 값으로 교체해야 합니다.

```sql
begin;
update public.delivery_jobs set status = 'sending'
where source = 'LH' and post_id = '실제공고ID' and status = 'uncertain';
select public.complete_delivery(
  'LH', '실제공고ID', '00000000-0000-0000-0000-000000000000'::uuid, 12345
);
commit;
```

## 검증과 운영상 한계

- Python 오프라인 테스트는 필터, 실제 LH `data-id` 형식, SH 링크 변형, HWP 제어문자, HWPX, XLSX, 잘못된 JSON, 메시지 길이, 중복/전송 실패/DB 실패 경로를 검증합니다.
- SQL 검증은 로컬 메모리 PostgreSQL(PGlite)에서 실제 `schema.sql`을 실행하고 예약 충돌, 예약 만료, 전송 후 insert, 완료 함수 재호출, 불확실 상태 보류, RLS/권한을 확인합니다. **원격 Supabase Data API 통합 테스트를 대신하지는 않습니다.**
- 2026-09-27 LH 실접속 dry-run: 최근 14일 공고 89건의 목록을 확인한 뒤 서울 청년매입임대 1건의 PDF·HWPX·XLSX 첨부를 파싱했습니다. 본문 포함 152,158자의 텍스트를 추출했고 LLM·DB·텔레그램은 호출하지 않았습니다. 목록 페이지 이동 조건(`srchY`)도 실응답으로 확인했습니다.
- SH는 이 개발 환경에서 오류 페이지로 리다이렉트되어 **실제 목록·상세·첨부 동작을 검증하지 못했습니다.** 서울시 공식 주거포털에서 게시판 주소를 확인했으며, SH 파서는 예제 HTML 테스트까지만 완료했습니다. 배포 환경에서 `--source SH --dry-run` 결과를 확인하고 필요시 선택자를 조정해야 합니다. 현재 SH까지 운영 검증이 끝난 봇으로 간주하면 안 됩니다.
- 실제 OpenAI 요약, 원격 Supabase 기록, Telegram 전송은 사용자의 키와 대상 설정 후 검증해야 합니다. 이 코드 생성 과정에서는 실행하지 않았습니다.
- 사이트 차단/구조 변경은 정상 “공고 없음”으로 숨기지 않습니다. 다른 사이트·공고는 계속 처리하되 마지막 종료코드는 1이 되어 Actions 실패로 표시됩니다. 첨부 일부 실패는 공고 처리 자체를 중단하지 않고 메시지에 한계를 표시합니다.
- 실패 공고는 설정이 고쳐질 때까지 재시도하므로 반복 API 비용이 발생할 수 있습니다. Actions 로그와 `delivery_jobs`를 확인하세요.

선택 사항인 SQL 검증을 다시 실행하려면 **터미널**에서 Node.js로:

```powershell
npm install --prefix .scratch/sql-check --ignore-scripts --save-exact @electric-sql/pglite@0.5.8
node tests/check_schema.mjs
```

## 참고한 공식 자료

- [LH 임대주택 공고 목록](https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancList.do?mi=1026)
- [서울주거포털 SH 공공임대](https://housing.seoul.go.kr/site/main/sh/publicLease/04/list)
- [Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [GPT-4o 모델·가격](https://developers.openai.com/api/docs/models/gpt-4o)
- [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage)
- [GitHub Actions schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
