# Tarmac → Cachegrind

처음 사용한다면 [INTRO.html](INTRO.html)의 **한국어 / English 빠른 시작**을 참고하세요.
저장소를 내려받고 브라우저에서 열면 언어 전환과 명령어 복사를 사용할 수 있습니다.
외부 인터넷 연결은 필요하지 않습니다. GitHub 파일 페이지에서는 HTML 소스가 표시됩니다.

두 가지 Arm Tarmac 로그 형식의 **PC별 명령어 실행 횟수**를 ELF와 결합해
PC·함수·파일·소스 라인별 **flat 프로파일**로 변환합니다.

Python 표준 라이브러리와 대상 코어용 `objdump`를 사용하며, 소스 위치 조회에는
GNU 호환 `addr2line`을 우선 사용합니다. 자동 탐색으로도 찾지 못하면 objdump의
라인 번호 출력을 파싱합니다.
기본적으로 ELF의 명령어 주소를 `Ir=0`으로 포함하고 trace 실행 횟수를 더합니다.
로그는 한 줄씩 읽고, ELF/trace의 고유 PC별 횟수를 모읍니다. addr2line을 사용하는
경우 주소를 일괄 질의합니다.
전체 로그를 메모리에 올리거나 명령어 실행마다 외부 프로세스를 만들지 않습니다.

개별 프로파일의 출력 이벤트는 `Ir` 하나입니다. 배치 total에는 아래 설명한 테스트 커버리지 이벤트도 포함됩니다. **call graph, 호출 횟수, inclusive cost,
실행 시간, 캐시 hit/miss는 추정하지 않습니다.** Interrupt handler의 명령어도
해당 PC의 함수/소스에 직접 귀속되므로 호출 스택 복원이 필요하지 않습니다.

## 요구사항

- Python 3.6.8 이상. `pip install` 불필요.
- 해당 ELF를 읽을 수 있는 `addr2line`:
  `arm-none-eabi-addr2line`, `llvm-addr2line`, `addr2line` 순으로 자동 탐색합니다.
  여러 toolchain이 있으면 `--addr2line`으로 직접 지정하세요.
  이 옵션을 생략하고 위 실행 파일을 모두 찾지 못한 경우에는 `objdump -l`로 전환합니다.
- 해당 ELF를 디스어셈블할 수 있는 `objdump`. 선택된 `addr2line`과 같은 toolchain을
  우선 탐색하며, `--objdump arm-none-eabi-objdump`처럼 직접 지정할 수 있습니다.
  호스트용 `objdump`가 Arm을 지원하지 않으면 Arm toolchain을 지정하세요.
- 실행 이미지에 대응하는 ELF. 함수명에는 심볼, 파일/라인에는 DWARF 디버그 정보가
  필요합니다. 실제 사용한 최적화 옵션을 유지하고 `-g`를 포함한 ELF를 사용하세요.
- 결과 확인용 `positions: instr line`을 지원하는 뷰어.

## 빠른 시작: 두 코어를 각각 변환

```bash
python3 tarmac_to_cachegrind.py core0.log \
  --elf core0.elf \
  --addr2line arm-none-eabi-addr2line \
  --objdump arm-none-eabi-objdump \
  -o cachegrind.out.core0

python3 tarmac_to_cachegrind.py core1.log \
  --elf core1.elf \
  --addr2line arm-none-eabi-addr2line \
  --objdump arm-none-eabi-objdump \
  -o cachegrind.out.core1

# 생성한 파일을 PC + 소스 라인 형식을 지원하는 뷰어에서 여세요.
```

형식은 기본적으로 자동 인식합니다. 필요하면 `--format es` 또는 `--format it`로
제한할 수 있습니다. 제한한 형식과 다른 명령어 이벤트가 나오면 오류로 처리합니다.

**한 번의 실행에 한 코어의 로그와 대응 ELF를 넣으세요.** 서로 다른 코어/이미지의
로그를 합치면 동일한 PC 주소를 구분할 수 없습니다. 같은 코어의 두 문법을 자동
인식하는 것과 다중 코어를 식별하는 것은 별개입니다. 코어 태그가 추가된 통합 로그는
현재 지원하지 않습니다. 코어별 flat 실행 횟수를 만들 때는 `tic`/`ps` 시간축 정렬이
필요하지 않습니다.

## 여러 시나리오 일괄 변환 및 merge

배포할 파일은 **`tarmac_to_cachegrind.py` 하나**입니다. 입력이 로그 파일이면 단일
변환, 폴더이면 깊이가 제한된 배치 변환으로 자동 선택합니다. 기존 별도 배치 파일은 통합되어
제거됐으므로 배치 실행 명령도 이 파일명으로 바꿔 주세요. 배치에서는 ELF
디스어셈블/소스 매핑을 공유합니다. trace에서 새롭게 발견된
PC만 추가 조회합니다. 기본은 순차 실행이며 `--workers N`으로 파일 단위 병렬 처리를 켭니다.

```bash
python3 tarmac_to_cachegrind.py /work/test_results \
  --elf /work/fw/core0.elf \
  --objdump arm-none-eabi-objdump \
  --output-dir /work/coverage/run001
```

- 기본적으로 입력 폴더와 바로 아래 하위 폴더까지만 탐색합니다(`--max-depth 1`).
  `--max-depth 0`은 입력 폴더만, `--max-depth 2`는 두 단계 아래까지 탐색합니다.
  깊이 제한에 도달하면 더 아래 폴더를 열지 않습니다. 기본 파일명 패턴은 `tarmac*.log` 및
  `tarmac*.log.gz`이며 대소문자를 구분합니다. 심볼릭 링크는 따라가지 않습니다.
- 먼저 파일명으로 후보를 좁히고 실제 변환 파서로 검증합니다. 모든 `.log`의 내용을
  별도로 탐색하는 이중 읽기를 하지 않습니다. `build.log` 등은 무시하며, 후보 파일에
  Tarmac 명령어/예외 이벤트가 없거나 명령어 형식이 잘못됐으면 실패 목록에 기록합니다.
- 기본 출력 폴더는 `상위폴더/cachegrind-output`입니다. **기존 폴더를 그대로 사용할 수
  있고 같은 이름의 결과 파일은 성공 시 교체합니다.** 이번 실행에서 선택하지 않은 파일은
  유지합니다. merge는 과거 출력 파일을 읽지 않고 이번 실행에서 성공한 로그만 합산합니다.
- 선택된 로그를 상대 경로순으로 정렬하고 1부터 인덱스를 부여합니다.
  파일명은 `인덱스_폴더_로그이름_cachegrind.out`입니다. 폴더는 입력 루트 기준
  상대 경로를 `_`로 연결합니다. 루트 바로 아래 로그에는 입력 루트의 폴더명을 붙입니다.
  `.log`/`.log.gz`는 제거합니다. 병렬 완료 순서가 달라도 번호는 동일합니다.

| 입력(상위폴더 기준) | 출력 파일명 |
|---|---|
| `case01/tarmac_core0.log` | `1_case01_tarmac_core0_cachegrind.out` |
| `case02/tarmac_core0.log` | `2_case02_tarmac_core0_cachegrind.out` |
| `group/case03/tarmac_core0.log.gz` | `3_group_case03_tarmac_core0_cachegrind.out` |

기본 깊이에서는 위 표의 `group/case03` 로그는 제외됩니다. 이 경로까지 포함하려면
`--max-depth 2`를 지정하세요. 파일명 조건에 맞지 않는 파일은 파일 정보 조회를
최소화하고, 모든 경로에 `resolve()`를 호출하지 않습니다.

끝에는 **`total_merge_cachegrind.out`**, **`coverage_index.json`**, **`batch_report.json`**도 생성됩니다.
merge는 변환 중 메모리에서 동일한 runtime PC의 `Ir`을 합산한 뒤 공유 ELF의
파일·함수·라인에 연결합니다. 개별 출력
파일 800개를 다시 읽는 post-merge 단계가 없습니다. 미실행 라인의 0은 유지되며,
어느 시나리오에서든 실행한 라인은 합계가 양수가 됩니다. `Ir`은 실행 횟수의 합계이지
그 라인을 실행한 시나리오의 개수가 아닙니다.

개별 파일이 필요 없고 최종 merge만 보고 싶다면 `--merge-only`를 추가하세요.
개별 프로파일 쓰기를 생략해 디스크 작업을 줄입니다.

```bash
python3 tarmac_to_cachegrind.py /work/test_results \
  --elf /work/fw/core0.elf --pattern 'tarmac_core0*.log' \
  --merge-only -o /work/coverage/core0-merged
```

`--pattern`은 파일 basename에 적용하며 여러 번 지정할 수 있습니다. 지정하면 기본
패턴을 대체합니다. 셸에서 먼저 확장되지 않도록 **따옴표로 감싸세요.** `.log.gz`도
선택하려면 해당 패턴을 추가하세요. `--addr2line`, `--objdump`, `--load-offset`,
`--format`, `--executed-only`도 단일 변환기와 같은 방식으로 사용할 수 있습니다.
addr2line이 자동 탐색되지 않으면 objdump의 라인 정보를 재사용합니다.

### 코어별 ELF가 다를 때: 정확한 파일명 선택

**한 배치 실행은 동일한 ELF와 주소 매핑을 사용하는 로그만 선택해야 합니다.**
각 폴더에서 두 코어의 로그 이름이 일정하다면 `--log-name`을 사용하는 것이 적절합니다.
파일명 전체를 대소문자까지 정확히 비교하며 경로나 glob 패턴으로 해석하지 않습니다.
기존 `--pattern`과는 동시에 지정할 수 없습니다. 둘 다 생략하면 기존 기본 패턴으로
모든 후보를 선택하므로, ELF가 다른 두 로그가 있으면 반드시 선택 옵션을 지정하세요.

```bash
python3 tarmac_to_cachegrind.py /work/test_results \
  --elf core0.elf --log-name tarmac_core0.log --workers 4 -o /work/coverage

python3 tarmac_to_cachegrind.py /work/test_results \
  --elf core1.elf --log-name tarmac_core1.log --workers 4 -o /work/coverage
```

두 실행에 **같은 출력 폴더**를 지정할 수 있습니다. `--log-name`을 지정한 경우
`.log` 또는 `.log.gz`를 제거한 이름으로 merge/보고서를 구분합니다.

| 선택 옵션 | 합산 파일 | 보고서 |
|---|---|---|
| `--log-name tarmac_core0.log` | `total_merge_tarmac_core0_cachegrind.out` | `batch_report_tarmac_core0.json` |
| `--log-name tarmac_core1.log` | `total_merge_tarmac_core1_cachegrind.out` | `batch_report_tarmac_core1.json` |
| 미지정 (`--pattern`만 사용한 경우 포함) | `total_merge_cachegrind.out` | `batch_report.json` |

같은 코어를 다시 실행하면 이번에 생성하는 개별 파일·합산·목록·보고서를 교체합니다. 기존 결과에
중복 누적하지 않습니다. `--log-name`으로 `.log`와 그 압축본 `.log.gz`를 각각 실행하면
merge/목록 태그가 같으므로 별도로 보관하려면 출력 폴더를 분리하세요.
한 배치에서 원본과 압축본을 모두 선택하면 각각 별도 테스트로 집계합니다. 옵션에 적은 파일이 실제로 선택한
ELF와 대응하는지는 사용자가 확인해야 하며, 파일명만으로 자동 검증할 수 없습니다.

### Total에서 커버한 테스트와 커버하지 않은 테스트 확인

추가 옵션 없이 배치 total에 다음 이벤트를 생성합니다. 단일 변환과 개별 프로파일은
기존 `Ir` 형식을 유지합니다. `--merge-only`에서도 total과 인덱스 목록을 생성합니다.

```text
events: Ir Tests Covered1 Covered2 Covered3 Covered4 Covered5 CoveredSet Uncovered1 Uncovered2 Uncovered3 Uncovered4 Uncovered5 UncoveredSet
```

| 이벤트 | 의미 |
|---|---|
| `Tests` | 해당 소스 라인의 명령어를 한 번 이상 실행한 정상 처리 테스트 수 |
| `Covered1` ~ `Covered5` | 커버한 테스트의 인덱스, 오름차순 앞 5개 |
| `CoveredSet` | 앞 5개를 제외한 나머지 커버 인덱스 목록의 번호 |
| `Uncovered1` ~ `Uncovered5` | 커버하지 않은 정상 처리 테스트의 인덱스, 오름차순 앞 5개 |
| `UncoveredSet` | 앞 5개를 제외한 나머지 미커버 인덱스 목록의 번호 |

빈 슬롯과 나머지 목록이 없는 경우는 `0`입니다. 테스트 ID와 집합 ID는 각각 독립적인
번호이며 둘 다 1부터 시작합니다. 같은 나머지 목록에는 같은 집합 번호를 사용합니다.
전체 450개 중 테스트 17, 305만 실행하지 않은 라인은 `Tests=448`,
`Uncovered1=17`, `Uncovered2=305`, `Uncovered3~5=0`, `UncoveredSet=0`입니다.

목록 파일 이름은 `coverage_index.json`이며, `--log-name tarmac_core0.log` 사용 시
`coverage_index_tarmac_core0.json`입니다. 두 코어를 같은 폴더에 출력해도 구분됩니다.
목록에는 다음과 같은 내용이 들어갑니다(일부 항목을 생략한 예시).

```json
{
  "tests": [
    {"index": 1, "input": "case001/tarmac_core0.log",
     "output": "1_case001_tarmac_core0_cachegrind.out", "status": "successful"}
  ],
  "sets": {
    "42": [6, 8, 14]
  }
}
```

`CoveredSet` 또는 `UncoveredSet`이 42이면 `sets["42"]`의 목록을 확인합니다.
이 목록에는 **슬롯에 이미 표시한 앞 5개가 포함되지 않습니다.** `tests` 목록에서 각
인덱스의 실제 입력 경로와 출력 이름을 찾을 수 있습니다. `--merge-only`에서는 출력
파일이 없으므로 `output`은 null이며 예정 이름은 `planned_output`에 기록합니다.

변환 실패한 테스트는 커버·미커버 양쪽에서 제외하고 `status=failed` 및 오류를 기록합니다.
실패한 번호는 비워두며 뒤의 번호를 당겨 재배정하지 않습니다. 아무 명령어도 실행하지
않았지만 EXC/IS 이벤트가 있는 정상 로그는 실행된 라인의 미커버 테스트로 포함합니다.
번호는 **이번 선택 목록 안에서만** 유효합니다. 로그 추가·삭제나 선택 옵션 변경 시 번호가
달라질 수 있으므로 현재 total에 대응하는 목록을 사용하세요. 이전 버전의 파일명으로 생성된 파일이나
이번 실행에서 생성하지 않은 과거 파일은 자동 삭제하지 않습니다.

#### 소스 화면과 Assembly 화면의 이벤트 해석

`Tests`, `Covered1~5`, `CoveredSet`, `Uncovered1~5`, `UncoveredSet`은
**소스 라인 단위 정보**입니다. 같은 `(파일, 라인 번호)`의 모든 PC에서 실행 테스트
집합을 합칩니다. 같은 테스트가 여러 명령어를 실행해도 `Tests`는 한 번만 증가합니다.
미커버는 정상 처리 테스트 중 그 라인의 명령어를 하나도 실행하지 않은 테스트입니다.

추가 이벤트는 그 라인의 **실행된 PC 중 출력 순서상 첫 번째 PC 하나**에만 기록합니다.
나머지 PC에는 0을 넣어 뷰어가 PC별 값을 소스 라인으로 합산할 때 중복되지 않게 합니다.
`Ir`은 모든 PC에 실제 실행 횟수를 그대로 기록합니다.

**PC의 total `Ir=0`이면 추가 이벤트도 모두 0입니다.** 라인 전체가 미실행이면
Covered와 Uncovered 목록을 모두 생략하고 이벤트를 전부 0으로 기록합니다.
라인의 첫 명령어가 미실행이고 뒤의 명령어가 실행됐다면, 뒤의 실행된 명령어를 대표로
사용합니다. 소스 위치를 모르는 PC는 가상의 `???:0` 하나로 합치지 않고 별도로 처리합니다.

| 뷰어 화면 | 참조할 이벤트 |
|---|---|
| Source code | `Ir`, `Tests`, Covered/Uncovered 슬롯과 Set 번호 |
| Assembly / instruction | **`Ir`만 참조** |

**Assembly 화면의 `Ir` 이외 이벤트는 해당 명령어의 커버 정보를 뜻하지 않습니다.**
값이 있는 PC는 라인 정보를 저장하는 대표 위치일 뿐이고, 0인 PC도 실행됐을 수 있습니다.
추가 이벤트의 테스트 번호·집합 번호는 Source code 화면에서 확인하세요.

함수 전체 합계·summary·백분율의 인덱스 값에는 의미가 없습니다. `Tests` 함수 합계도
함수의 고유 테스트 수가 아닙니다. 커버리지 색상은 `Ir`을 기준으로 확인하세요.
`Covered5=0`은 다섯 번째 테스트가 없다는 뜻이므로 미실행 판정에 사용하면 안 됩니다.
이 방식은 뷰어가 PC별 값을 소스 라인으로 합산하는 것을 전제로 합니다.
실제 사용자 뷰어의 표시는 별도 확인이 필요합니다.

목록 JSON은 `version=3`, `coverage_unit="source_line"`이며 Assembly 해석 가이드도
포함합니다. 집합 ID 조회 방식은 동일합니다. Total과 목록은 함께 재생성하세요.

#### 추가 처리 비용

부모가 파일별 sparse PC 집계에 테스트 비트를 OR하고, 최종 total 생성 때 소스 라인별로 합쳐
대표 실행 PC에 기록합니다. 성공 테스트 집합의 차집합으로 미커버를 계산합니다. 명령어마다 공유 락을
사용하지 않고, 로그를 다시 읽거나 450개의 개별 출력을 post-merge하지 않습니다.
동일한 커버 집합의 이벤트 문자열과 나머지 목록은 재사용합니다.

보고서의 `coverage_seconds`는 테스트 멤버십 추가와 최종 확장 total/목록 생성 시간을
포함합니다. `merge_seconds`에 포함되는 시간이므로 두 값을 더하면 안 됩니다.
이 값은 새 기능의 순수 증가분이 아니며, 전체 생성 시간 증가는 변경 전후 실행으로
비교해야 합니다. 재현 가능한 비교와 측정값은 [성능 결과](tests/COVERAGE_BENCHMARK.md)에
정리합니다.

### 속도 개선과 병렬 실행

`--workers 4`는 최대 4개의 **프로세스**가 각각 로그 하나를 파싱하고 개별 출력을
쓰도록 합니다. Python 3.6의 GIL 때문에 CPU 파싱을 여러 코어에 분산하려면 스레드보다
프로세스가 적합합니다. 기본값은 1이고, 지정한 수가 로그 수보다 크면 로그 수로 제한합니다.
두 코어의 변환 명령을 동시에 실행하면 `4 + 4 = 8`개 worker가 사용됩니다.
우선 4개로 측정한 다음 가용 CPU·메모리·스토리지 처리량에 맞춰 조절하세요.

| 처리 단계 | 기존 | 변경 |
|---|---|---|
| 로그 파싱 | 모든 줄의 이벤트 검사, 매 명령어 정규식 해석 및 PC 숫자 변환 | 비이벤트 줄 빠른 제외, 반복 명령어 해석 결과 최대 8,192개 LRU 캐시 |
| ELF 분석 | 배치 시작에 한 번 분석 | 동일하게 한 번 분석하고 worker에 전달 |
| 개별 출력 | ELF 전체 0 비용 복사, 위치 키 구성, 매번 정렬·문자열 생성 | 기본 coverage 출력 순서와 PC/라인 문자열을 한 번 준비해 재사용 |
| total 집계 | 각 파일의 전체 coverage 항목을 0까지 합산 | 성공한 파일의 실행 PC별 횟수만 부모에서 합산 |
| 파일 처리 | 순차 | `--workers`로 제한한 프로세스 병렬 처리 |

파서 캐시는 timestamp를 제외한 이벤트 종류와 **본문 전체**를 키로 사용합니다.
같은 PC라도 opcode, CCFAIL, metadata 등이 다르면 별도로 해석하며, 캐시가 적중해도
각 실행 횟수는 모두 증가합니다. 본문이 매번 달라지는 trace에서는 캐시 효과가 작고
캐시 관리 비용 때문에 파싱 자체는 오히려 느려질 수도 있습니다.
로그 전체는 계속 스트리밍하고 캐시는 파일마다 해제합니다.

기본 ELF에 없는 PC가 실행되면 해당 출력에만 추가 위치를 구성합니다. 이 경우의 출력
정렬 비용은 남습니다. `--executed-only`는 파일마다 실행 PC 집합이 달라 개별 정렬이
필요합니다. 미실행 PC를 포함하는 기본 동작, PC·라인 구분, 헤더는 유지합니다.

동시성 안전성은 다음과 같이 확보합니다.

- worker마다 독립적인 실행 횟수·소스 조회 캐시를 갖고, 고유 인덱스를 부여한
  서로 다른 출력 파일만 씁니다. 임시 파일 기록 후 atomic replace가 성공해야 완료로 처리합니다.
- worker는 sparse PC 횟수와 통계만 반환합니다. 부모만 total을 수정하고 마지막에 한 번
  기록하므로 **명령어마다 공유 카운터를 잠그는 critical section이 없습니다.**
- 제출했지만 아직 부모가 반영하지 않은 작업은 최대 `2 × workers`개입니다.
  파일 하나가 늦어도 다른 완료 결과부터 반영하고 새 작업을 공급합니다. 프로세스 간 통신과
  부모의 집계 비용은 남지만, 원본 로그나 ELF 전체 0 비용을 매 파일마다 전송하지 않습니다.
- Linux 기본 `fork`에서는 사전 계산한 ELF/출력 레이아웃을 copy-on-write로 공유합니다.
  `spawn` 환경에서도 initializer로 worker마다 한 번 전달합니다. 추가로 발견한 PC의
  소스 정보는 worker별로 조회하므로 같은 미등록 PC가 여러 worker에서 조회될 수 있습니다.
- 진행 표시는 부모가 완료된 순서로 출력하므로 병렬 실행 시 파일명 순서와 다를 수 있습니다.
  최종 프로파일과 보고서의 성공/실패 목록은 완료 순서에 영향받지 않습니다.

보고서에는 파일별 `seconds.parse/map/write/total`, 실제 worker 수 `workers`,
부모 집계와 최종 total 생성 시간 `merge_seconds`, 보고서 쓰기 직전까지의 전체 시간
`elapsed_seconds`가 추가됩니다. 병렬 작업의 파일별 시간을 합치면 전체 경과 시간보다
클 수 있습니다. 파싱보다 쓰기 시간이 길다면 worker를 늘리기보다는 로컬 SSD 출력이나
개별 출력이 필요 없는 경우 `--merge-only`를 검토하세요. 기본 coverage 출력 크기 자체는
줄이지 않으므로 스토리지가 병목이면 CPU 수에 비례한 향상을 기대할 수 없습니다.

재현 가능한 합성 성능 비교(실행용으로 배포할 파일은 여전히 하나):

```bash
git show 475cadf:tarmac_to_cachegrind.py > /tmp/tarmac_before.py
python3 tests/benchmark_batch.py --baseline /tmp/tarmac_before.py --workers 4
```

GCC로 4,000개 함수의 디버그 ELF를 만들고, ES/IT 로그 총 12개에 각각 실행 명령어
20만 개와 레지스터/메모리 기록을 넣습니다(총 720만 줄). 기존 순차·개선 순차·개선 병렬의
전체 변환 시간을 측정하고 **개별 12개 + total 1개의 PC·소스·Ir 일치**를 검증합니다.
준비 데이터와 결과는 종료 후 제거됩니다. 도구 실행·파싱·coverage 출력·merge를 포함하며
로그 생성 시간은 제외합니다. 캐시와 장비 부하에 영향받는 합성 측정이며 실제 Arm 로그의
수분~십수분 완료를 보장하지 않습니다.

이번 개발 환경에서 위 기본 인자로 측정한 1회 결과입니다.

| 인터프리터 | 기존 순차 | 개선 순차 | 개선 4 workers | 기존 대비 병렬 향상 |
|---|---:|---:|---:|---:|
| Python 3.6.8 | 72.79초 | 30.82초 | 10.67초 | 6.82배 |
| Python 3.12.14 | 18.47초 | 7.90초 | 5.22초 | 3.54배 |

3.6.8은 호환성 확인용으로 `CFLAGS=-O0`로 빌드한 인터프리터입니다. 위 표는 같은
인터프리터 내 알고리즘/worker 비교용이며, Python 버전 간 속도 비교나 사용자 장비의
절대 시간 예측에는 사용할 수 없습니다. 실제 로그에서는 우선 일부 폴더를 대상으로
`--workers 1`, `4`, `8`의 보고서 경과 시간을 비교하세요.

### 병렬 진행 상황 출력

배치 시작 시 `[0/450] starting ...`, 파일 완료 시 `[3/450] complete "3_case003_tarmac_core0_cachegrind.out"`
형식으로 표시합니다. 병렬 worker의 다음 결과를 기다리는 동안에는 5초마다 현재 처리 완료 수와
경과 시간을 출력합니다. 이 수에는 성공과 실패가 모두 포함되며 실행 중인 파일을 완료로 세지 않습니다.

```text
[0/450] starting with 4 worker process(es)
[0/450] waiting for workers; 5.0s elapsed
[1/450] complete "3_case003_tarmac_core0_cachegrind.out"
[2/450] complete "1_case001_tarmac_core0_cachegrind.out"
[2/450] waiting for workers; 15.0s elapsed
...
[450/450] generating merged profile and coverage index...
```

대괄호 안 숫자는 완료 건수이고 파일명 앞 숫자는 테스트 ID이므로 병렬 실행에서는 서로 다를 수 있습니다.
`--merge-only`에서도 같은 건수를 표시하며 파일별 동작 이름은 `processed`입니다.
마지막 `450/450` 이후에도 total과 목록 생성이 남아 있으므로 최종 `Completed:`를 확인하세요.

기본 진행 출력은 `stderr`이며 모든 메시지를 즉시 flush합니다. 실행 도구가 stdout만 수집하거나
`tee`로 진행 상황을 보고 싶다면 `--progress-stream stdout`을 추가하세요. 오류/경고는 계속 stderr입니다.

```bash
python3 tarmac_to_cachegrind.py /work/test_results --elf core0.elf \
  --log-name tarmac_core0.log --workers 4 --progress-stream stdout -o /work/coverage
```

5초 표시는 부모가 결과 큐를 기다릴 때만 동작합니다. 파일 내 진행률을 추정하지 않으며,
새로운 감시 스레드나 명령어 단위 메시지 전송을 추가하지 않습니다.

### 실패 처리

각 파일의 성공/실패 및 통계는 `batch_report.json`에 남습니다. 잘못된 로그가 있어도
나머지를 계속 변환하며, 실패가 하나라도 있으면 종료 코드는 1입니다. 일부만 성공하면
성공한 로그의 결과로 total을 생성하고 stderr에 **PARTIAL MERGE** 경고를 표시하며
JSON의 `partial_merge`를 true로 기록합니다. 출력 프로파일에는 `desc:`를 넣지 않습니다.
모두 실패하면 새로운 total은 만들지 않습니다. 이전 total이 있으면 보존하되 갱신되지
않았다는 경고를 출력합니다. 실패한 개별 파일의 이전 결과도 보존하고 이번 merge에는
넣지 않습니다. 이렇게 보존된 파일은 보고서의 `preserved_previous_outputs`에서 확인할
수 있습니다. `merged_output: null`이면 이번 실행에서 생성한 merge가 없습니다. ELF 분석 실패 등 공통 오류는
변환 시작 전에 중단합니다. 실행 명령어가 0개라도 EXC/IS 같은 인식 가능한 이벤트가
있으면 기본 coverage 모드에서 0으로 처리하지만, 빈 파일이나 무관한 텍스트는 실패합니다.

### 진행 상황 출력

파일 저장이 끝나면 다음과 같이 stderr에 즉시 출력합니다(`flush=True`).
출력을 파일로 리다이렉트해도 각 메시지를 바로 기록합니다.

```text
Searching /work/test_results (max-depth=1)...
Search complete: 800 logs in 0.25s
Found 800 logs; analyzing ELF...
ELF analysis complete in 3.10s
[1/800] complete "1_case001_tarmac_core0_cachegrind.out"
[2/800] complete "2_case001_tarmac_core1_cachegrind.out"
...
[800/800] complete "800_case400_tarmac_core1_cachegrind.out"
complete "coverage_index.json"
complete "total_merge_cachegrind.out"
complete "batch_report.json"
Completed: 800 succeeded, 0 failed; ...
```

위 시간은 표시 형식을 설명하기 위한 예시입니다. 검색과 ELF 분석 시간을 따로 표시하므로
두 단계 중 어느 쪽에서 지연되는지 확인할 수 있습니다.

실패한 파일은 `FAILED`로 출력하고 complete로 표시하지 않습니다. `--merge-only`에서는
개별 파일을 생성하지 않으므로 각 입력의 합산이 끝나면 `processed "입력파일"`로 표시하며,
최종 파일 저장 후에는 `complete`를 출력합니다. 단일 로그 변환도 저장 후 complete를
출력합니다. 로그 한 개를 읽는 도중의 바이트/퍼센트 진행률은 출력하지 않습니다.

`-o`는 단일 모드에서 출력 파일, 폴더 모드에서 출력 디렉터리입니다. 배치에서는
`--output-dir`도 사용할 수 있습니다. `--pc-counts`, `--stats`, `--skip-malformed`는
단일 로그 전용이며 배치는 `batch_report.json`에 파일별 통계를 기록합니다.

## 기존 결과에서 일부 테스트만 선택해 다시 merge

`--merge-list`는 **이미 생성한 개별 Cachegrind 파일**을 읽어 새 total과 테스트 인덱스를
생성합니다. Tarmac 재파싱이나 ELF/objdump/addr2line 실행이 필요하지 않습니다.
기존 `--merge-only`는 Tarmac을 변환하면서 개별 출력 생성을 생략하는 옵션으로, 용도가 다릅니다.

예를 들어 `selected.txt`에 결과 폴더를 적습니다.

```text
# 폴더 또는 개별 파일을 한 줄에 하나씩 지정
/work/coverage
```

제외할 결과는 선택적으로 `excluded.txt`에 적습니다.

```text
/work/coverage/17_case017_tarmac_cm4_cachegrind.out
/work/coverage/35_case035_tarmac_cm4_cachegrind.out
```

```bash
python3 tarmac_to_cachegrind.py \
  --merge-list selected.txt \
  --merge-pattern '*_tarmac_cm4_cachegrind.out' \
  --exclude-list excluded.txt \
  -o /work/coverage/selected_cm4_cachegrind.out
```

제외할 항목이 없으면 `--exclude-list`를 생략하세요. 폴더마다 같은 이름의 파일을
가져오려면 `--merge-name core0_cachegrind.out`처럼 지정할 수 있습니다.
`--merge-name`은 `--merge-pattern`의 별칭이며 정확한 파일명과 wildcard를 모두 받습니다.
숫자 접두사가 없는 파일은 아래 설명한 `--reindex`가 필요합니다.

개별 파일만 직접 나열할 때에는 패턴 옵션이 필요하지 않습니다. 파일과 폴더를
한 목록에 섞어도 됩니다.

```text
/work/coverage/1_case001_tarmac_cm4_cachegrind.out
/work/coverage/8_case008_tarmac_cm4_cachegrind.out
```

```bash
python3 tarmac_to_cachegrind.py --merge-list selected.txt \
  -o /work/coverage/selected_cm4_cachegrind.out
```

### 목록과 번호 처리

- UTF-8 텍스트이며 BOM, 빈 줄, `#`로 시작하는 주석 줄을 허용합니다.
  상대 경로는 **목록 파일이 있는 폴더** 기준입니다. 공백이 들어간 경로도 따옴표 없이
  한 줄에 그대로 적으세요. 경로 내부의 `#`는 주석으로 처리하지 않습니다.
- 폴더 항목은 그 폴더 **바로 안의 파일만** 읽습니다. 하위 폴더를 재귀 탐색하지 않습니다.
  폴더가 포함되면 `--merge-pattern`이 필수이며 여러 번 지정할 수 있습니다.
  셸에서 패턴이 먼저 확장되지 않도록 명령줄의 wildcard를 따옴표로 감싸세요.
- 제외 목록도 같은 규칙과 패턴을 사용합니다. 중복 지정한 파일과 같은 파일을 가리키는
  hard link는 한 번만 합칩니다. 폴더 탐색은 symlink를 따라가지 않지만 직접 지정한
  파일 symlink는 해석합니다.
- 기본적으로 `17_...`의 **원래 번호 17을 유지**합니다. 일부를 제외해도 나머지 번호는
  바뀌지 않습니다. 번호가 없거나 다른 파일과 중복되면 오류로 종료합니다.
- 여러 실행 묶음에 동일한 번호가 있으면 `--reindex`를 추가하세요. 선택한 파일의 정규화된
  절대 경로 정렬 순서로 1부터 번호를 다시 부여합니다. 입력 파일명은 변경하지 않으며,
  새 번호와 실제 입력 파일의 관계는 새 인덱스 JSON에 기록합니다.
- `Tests`, Covered/Uncovered 및 나머지 집합 번호는 **이번에 선택된 테스트만** 대상으로
  다시 계산합니다. 제외된 테스트는 Uncovered에도 나타나지 않습니다.
  소스 라인별 대표 실행 PC에만 추가 이벤트를 기록하고, `Ir=0`인 PC의 추가 이벤트는
  모두 0으로 유지합니다. Assembly 화면에서는 `Ir`만 instruction 단위로 해석하세요.

### 결과와 입력 검증

위 명령은 다음 세 파일을 생성하며 기존 동일 경로의 결과는 교체합니다.
출력 파일의 상위 폴더는 미리 존재해야 합니다.

```text
selected_cm4_cachegrind.out
selected_cm4_cachegrind.out.coverage_index.json
selected_cm4_cachegrind.out.merge_report.json
```

다른 코어는 다른 출력 이름을 지정하면 total 및 보조 파일이 충돌하지 않습니다.
집합 번호 해석에는 해당 total과 함께 생성된 인덱스 JSON을 사용하세요.
진행 상황은 `[3/450] merged "..."` 형태이며 `--progress-stream stdout`도 지원합니다.
이 모드는 기존 출력만 순차 집계하므로 `--workers` 병렬 변환 옵션을 사용하지 않습니다.

입력은 이 도구가 생성한 **`positions: instr line`, `events: Ir`의 개별 출력**입니다.
일반적인 모든 Callgrind 문법이나 이미 여러 이벤트를 가진 total의 재merge는 지원하지
않습니다. 폴더 탐색에서는 `total_merge`로 시작하는 파일과 이번 출력 경로를 자동 제외합니다.
그 외 이름의 과거 total이 패턴에 걸리면 오류가 나므로 개별 결과에 맞는 패턴을 사용하세요.

모든 입력의 `ob:` ELF 경로와 동일 PC의 파일·함수·라인 매핑을 비교하며 잘린 파일,
Ir summary 불일치 등을 거부합니다. 이는 ELF 바이너리 해시 검증은 아니므로 **같은 실행
이미지에서 생성한 결과**만 선택하세요. 입력 검증 실패 시 기존 결과를 교체하지 않습니다.
미실행 PC는 선택된 입력에 들어 있는 주소의 합집합으로 유지합니다. 입력을
`--executed-only`로 만들었다면 원래 빠져 있던 ELF 주소를 이 모드에서 복원할 수는 없습니다.

## 지원하는 로그

### ES / Tarmac Text Rev 3t

```text
Tarmac Text Rev 3t
         7095 tic ES EXC [1] Reset
                        R MSP 20020000
                        R XPSR f9000000
                        BR (000226f8) T
         7097 tic ES (000226f8:f2400000) T thrd: MOVW r0,#0
                              R R0 00000000
```

`ES (주소:인코딩)`에서 주소를 추출합니다. `ES EXC`는 명령어로 세지 않습니다.

### IT / ps

```text
23676374100 ps E Run
23676380700 ps R r14 ffffffff
23676384300 ps BNR4___I 00000000 00800320
23676384300 ps MNR4___I 00000000 00800320
23676405900 ps IT (00024812:00000000) 00024812 494e T16 LDR r1,[pc,#312] ; [0x2494c]
```

위 형식에서는 **괄호 다음의 명시적 PC `00024812`**를 사용합니다.
괄호 안의 `00000000`을 명령어 인코딩이나 실행 주소로 사용하지 않습니다.

다음 IT 계열 변형도 지원합니다. `IF`와 `IS`는 동일한 문법을 사용합니다.

```text
53 clk IT (53) 002109f0 f94003e1 O EL3h_s: LDR x1,[sp,#0]
54 clk IT 002109f4 910083e0 O EL3h_s: ADD x0,sp,#0x20
55 ps IT (00024812) 494e T16 LDR r1,[pc,#312]
56 ps IT (00024812:00000001) 494e T16 LDR r1,[pc,#312]
```

timestamp는 정수이며 생략할 수 있습니다. 단위는 `tic`, `ps`, `ns`, `clk`, `cs`,
`cyc`를 인식하고, 단위 없는 timestamp도 지원합니다. 명령어 state는 `T`, `T16`,
`T32`, `A`, `O`를 지원합니다. 상태 필드가 없는 다른 dialect는 지원하지 않습니다.

### 집계 규칙

| 레코드 | 처리 |
|---|---|
| 정상 `ES`, `IT` | 해당 PC의 `Ir`에 1 추가 |
| `IF` | 같은 cycle에 folded된 명령어도 1 추가 |
| `IS` | 조건 불충족이므로 제외 |
| `ES ... CCFAIL ...` | 조건 불충족이므로 제외 |
| `ES EXC ...` | 예외 이벤트로 집계하고 `Ir`에서 제외 |
| 인코딩이 `--------`, `....` 등인 레코드 | fetch 실패/알 수 없는 인코딩으로 제외 |
| `R`, `BR`, `E`, `BNR...`, `MNR...`, `LD`, `ST` 등 | 실행 명령어가 아니므로 제외 |
| 해석할 수 없는 `ES`/`IT`/`IF` 명령어 줄 | 기본적으로 줄 번호를 표시하고 변환 실패 |

`--skip-malformed`를 명시하면 해석 불가 명령어 줄을 생략하고 경고합니다.
기본 동작은 잘못된 형식으로 인한 누락을 숨기지 않도록 실패시키는 것입니다.
명령어 이벤트 외의 줄은 무시합니다. 실행 명령어가 하나도 없으면 기본 모드에서는
경고와 함께 ELF 기준의 전부 0인 프로파일을 생성합니다. `--executed-only`에서는 실패합니다.
같은 timestamp/PC가 반복돼도 각각 집계하며 임의로 중복 제거하지 않습니다.

**ES 주의사항:** Arm 문서에 따르면 일부 ES 생성기는 조건 실패 때 `CCFAIL`을
출력하지 않습니다. 이 변환기는 `CCFAIL` 없는 정상 ES 레코드를 실행된 것으로
가정합니다. 생성기가 조건 실패를 표시하지 않는 경우 `Ir`은 실제 실행 수보다
클 수 있으며, 이 스크립트는 레지스터 상태로 조건식을 다시 평가하지 않습니다.
원본 trace의 누락/중복도 자동으로 복구할 수 없습니다.

## 출력 의미

```text
# callgrind format
positions: instr line
events: Ir
ob: "firmware.elf"
fl=src/main.c
fn=main
0x202 75 0
0x204 76 1
0x206 76 399
summary: 400
```

`0x204 76 1`은 PC `0x204`가 소스 76번 라인에 대응하고 한 번 실행됐다는 뜻입니다.
같은 소스 라인의 다른 PC는 별도 행으로 유지합니다. 출력 PC는 trace의 runtime 주소이며,
ELF 조회에는 `PC - load_offset`을 사용합니다. 함수의 비용은
그 함수에 직접 귀속된 명령어 수의 합계이며, 하위 함수의 비용을 더하지 않습니다.
16비트와 32비트 명령어 모두 실행당 1입니다. 명령어 바이트 수나 cycle 수가 아닙니다.

### 미실행 코드와 coverage

기본 동작은 `objdump -d -z --no-show-raw-insn`으로 ELF의 실행 가능한 섹션에서
명령어 시작 주소를 수집하는 것입니다. 각 주소에 0을 설정하고 trace의 횟수를
합산하므로, 한 번도 호출되지 않은 함수나 실행되지 않은 라인도 `0x202 75 0`처럼 출력됩니다.
같은 라인의 다른 PC가 실행됐어도 미실행 PC의 값은 0으로 유지됩니다. `summary`에는
실행된 횟수만 합산되므로 0인 항목이 늘어도 총 실행 수는 변하지 않습니다.

메모리의 모든 바이트 주소를 PC로 취급하지 않습니다. 비실행 데이터 섹션은 제외하며,
코드 안에서도 objdump가 `.word`/`.short` 등으로 표시하는 데이터와 해석 불가 항목은
제외합니다. Arm/Thumb 구분과 코드 내 literal pool 식별을 위해 해당 아키텍처를
지원하는 objdump 및 mapping symbol이 보존된 ELF를 사용하는 것이 좋습니다.

**coverage의 범위는 ELF에 남아 있고 소스 라인으로 매핑 가능한 명령어입니다.**
최적화/링커에 의해 제거된 코드, 주석, 빈 줄은 0인 실행 가능 라인으로 추가하지 않습니다.
PC별 실행 여부를 확인할 수 있지만, 이 값만으로 완전한 branch/condition coverage를
나타내지는 않습니다.

CSV에도 미실행 PC를 `Ir=0`으로 포함합니다. JSON의 `unique_pcs`는 trace에서 실제로
실행된 고유 PC 수이고, `elf_instruction_pcs`는 objdump에서 수집한 고유 명령어 주소
수입니다. `--load-offset`은 ELF 주소를 runtime 주소로 변환하는 단계에도 적용됩니다.
뷰어가 0인 항목을 숨기면 표시 필터를 조정하세요. 요청된 출력은 `positions: instr line`을
사용하므로 기존 line-only Cachegrind 형식과 다릅니다. `cg_annotate`와 같은 line-only
reader는 지원하지 않을 수 있습니다. 첫 줄은 `# callgrind format`이며, `events: Ir` 다음에 요청된 `ob: "ELF 경로"`를
기록합니다. `desc:`와 `cmd:`는 출력하지 않습니다. `ob:` 표기는 사용자 뷰어용 요청
형식이며 일반 Callgrind의 object 지정 `ob=`와는 다릅니다.

기존처럼 실행된 PC만 처리하려면 다음과 같이 실행합니다. addr2line을 사용할 수 있으면
이 모드에는 objdump가 필요 없고 대형 ELF 전체를 탐색하지 않습니다. addr2line이 없으면
소스 위치를 얻기 위해 objdump가 필요하지만 출력에는 실행된 PC만 포함합니다.

```bash
python3 tarmac_to_cachegrind.py core0.log --elf core0.elf \
  --executed-only -o cachegrind.out.core0
```

미해결 파일/함수는 `???`, 미해결 라인은 `0`으로 기록해 비용을 보존합니다.
디버그 정보가 없는 ELF라도 함수 심볼이 있으면 함수별 집계가 가능할 수 있습니다.
심볼이나 소스 라인을 찾지 못한 실행 수는 stderr 및 선택적 JSON에 보고합니다.
PC가 ELF 범위를 벗어나도 해당 비용을 버리지 않습니다.

Inlining은 `addr2line -f -C`의 단일 결과에 한 번만 귀속합니다(`-i` 미사용).
인라인 호출 계층을 만들지 않습니다. 최적화로 인한 라인 매핑의 모호함, 심볼 alias,
별도 이미지/overlay, self-modifying code를 복원하지 않습니다.

### addr2line이 없는 환경

`--addr2line`을 생략하면 기존 순서로 자동 탐색합니다. 모두 없으면 stderr에 전환
메시지를 출력하고 `objdump -d -z --no-show-raw-insn -l -C`를 한 번 실행해 명령어
주소와 소스 위치를 함께 수집합니다. 추가 설치 없이 다음처럼 사용할 수 있습니다.

```bash
python3 tarmac_to_cachegrind.py core0.log --elf core0.elf \
  --objdump arm-none-eabi-objdump -o cachegrind.out.core0
```

fallback에서는 디스어셈블의 함수 심볼과 파일/라인 표기를 사용합니다. 함수·섹션이
바뀌면 이전 라인 정보를 초기화하며, objdump에 없는 trace PC는 주변 라인으로
추정하지 않고 `???`/라인 0으로 보존합니다. ELF에 디버그 정보가 없으면 objdump도
소스 라인을 복원할 수 없습니다. 인라인 함수/최적화 코드의 귀속은 addr2line 경로와
다를 수 있고, fallback의 함수명은 디스어셈블의 enclosing symbol을 기준으로 합니다.

명시적으로 지정한 `--addr2line` 경로가 없거나, 발견한 addr2line이 실행 중 실패하면
오류로 처리합니다. 자동 전환은 **옵션을 생략했고 자동 탐색에서도 찾지 못했을 때만**
발생합니다. objdump까지 없거나 실행에 실패하면 변환을 중단합니다.

## 진단 및 추가 옵션

```bash
python3 tarmac_to_cachegrind.py core0.log \
  --elf core0.elf -o cachegrind.out.core0 \
  --pc-counts core0-pcs.csv --stats core0-stats.json
```

CSV에는 `pc,elf_address,Ir,file,function,line`이 저장됩니다. ELF 주소와 소스 매핑을
검토하거나 특정 PC의 실행 횟수를 확인할 때 사용하세요. JSON에는 입력 줄 수,
이벤트별 레코드 수, 실행 명령어 수, 조건 불충족/예외/해석 실패 수와 미해결 비용이
포함됩니다. CSV·JSON도 전체 명령어 실행을 펼치지 않고 고유 PC 기준으로 기록합니다.

### 실행 주소가 ELF 주소와 다른 경우

```bash
python3 tarmac_to_cachegrind.py core0.log --elf core0.elf \
  --load-offset 0x20000000 -o cachegrind.out.core0
```

관계는 `ELF 주소 = trace PC - load_offset`입니다. 위 예시에서는 trace의
`0x20001000`을 ELF의 `0x1000`에 매핑합니다. 음수는 `--load-offset=-0x1000`처럼
지정할 수 있습니다. 옵션은 모든 PC에 적용되는 하나의 고정 offset이며, 여러 메모리
영역의 개별 재매핑은 지원하지 않습니다. 일반적인 Thumb trace PC는 실제 명령어
주소이므로 하위 비트를 임의로 지우지 않습니다.

### 압축 파일과 파이프

```bash
python3 tarmac_to_cachegrind.py core0.log.gz --elf core0.elf -o cachegrind.out.core0
gzip -dc core0.log.gz | python3 tarmac_to_cachegrind.py - --elf core0.elf -o cachegrind.out.core0
```

출력 파일의 부모 디렉터리는 미리 존재해야 합니다. 기존 출력 파일은 성공 시
교체됩니다. 각 파일은 임시 파일을 통해 기록되며, 파싱/심볼화 실패 시 기존 출력은
유지됩니다. 입력·출력 및 선택적 출력끼리 같은 파일을 지정할 수 없습니다.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```

Python 3.6.8 및 3.12.14 인터프리터에서 아래 62개 테스트의 통과를 확인했습니다.
`dataclasses` 등의 backport 패키지를 설치할 필요는 없습니다.

- 제공된 두 로그 문법을 바탕으로 만든 fixture: 명령어 추출, EXC/메모리 제외,
  조건 실패, folded 명령어, malformed 입력 처리.
- 실제 GCC 생성 ELF + `addr2line`: 함수/라인 연결, 두 문법의 출력 일치,
  offset, gzip/stdin, 미해결 주소 및 비용 보존.
- 미실행 함수의 0 비용, 데이터 제외, 같은 라인의 서로 다른 PC 보존 및 PC별 비용 합산, 빈 trace,
  실행 PC 전용 모드 및 잘못된 objdump 지정 시 기존 출력 보존.
- addr2line 없는 PATH에서 실제 objdump fallback: 두 trace 형식, offset, CSV,
  미실행 함수, 실행 PC 전용 모드, 미해결 PC 보존 및 명시적 도구 경로 오류.
- 800개 합성 로그의 일괄 변환/merge 및 ELF 분석 재사용, 인덱스 파일명, 패턴 필터,
  부분 실패 보고, 인덱스를 통한 이름 충돌 방지/덮어쓰기, merge 전용 모드 및 배치 fallback.
- 깊이 0/1/2의 탐색 결과, 제한 아래 폴더를 열지 않는지, 음수 깊이 거부를 검증합니다.
- 서로 다른 실제 ELF 두 개를 파일명으로 선택해 같은 폴더에 출력, 재실행 시 덮어쓰기,
  다른 코어 결과 보존, 정확한 파일명 비교 및 실패 시 과거 출력 보고를 검증합니다.
- 헤더 순서, PC/라인/Ir 열, 미실행 PC, runtime offset, merge 비용 보존을 검증합니다.
  사용자 뷰어 자체의 로딩 검증은 포함하지 않습니다.
- 실제 worker 프로세스에서 순차/병렬 출력 일치, gzip·미등록 PC·부분 실패·이전 출력
  보존·0 비용, spawn 모드의 merge-only/executed-only 및 잘못된 worker 수를 검증합니다.
- 같은 라인의 테스트 집합 합집합, 대표 실행 PC 선택, Ir=0의 추가 이벤트 0 처리, 450개 테스트의 커버/미커버 목록, 넘침 집합 재사용,
  실패 번호 제외 및 순차/병렬 total·인덱스 목록 일치를 검증합니다.
- 기존 출력 선택 merge: 목록 상대 경로·공백·BOM·중복·제외·비재귀 탐색, 원래 번호 유지와
  재번호 부여, 큰 번호의 밀집 비트마스크, ELF/매핑/summary 오류 및 기존 출력 보존을 검증합니다.
  800개 개별 출력의 재merge total이 최초 배치 total과 바이트 단위로 일치함을 확인합니다.
- 반복 해석 캐시가 CCFAIL·fetch 실패·IS/IF·malformed 및 실행 횟수를 보존하는지 검증합니다.

ELF 통합 테스트에는 `gcc`, `nm`, `addr2line`, `objdump`가 필요하며 없으면 해당 테스트가
skip됩니다. 테스트는 호스트에서 컴파일한 ELF 주소에 합성 Tarmac 이벤트를 연결하는
방식입니다. 실제 Arm 코어에서 수집한 전체 trace와 사용자 ELF의 검증을 대신하지는
않습니다. `tests/fixtures`의 로그는 짧은 파서 검증용 예시이며 대응 펌웨어 ELF를
포함하지 않습니다.

## 참고 문서

- [Arm Tarmac 이벤트 및 형식 설명](https://github.com/ARM-software/tarmac-trace-utilities/blob/main/doc/index.rst)
- [Cachegrind 출력 형식](https://valgrind.org/docs/manual/cg-manual.html#cg-manual.impl-details.file-format)
- [GNU addr2line](https://sourceware.org/binutils/docs/binutils/addr2line.html)
- [GNU objdump의 명령어 디스어셈블 옵션](https://sourceware.org/binutils/docs/binutils/objdump.html)
