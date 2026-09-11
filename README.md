# Tarmac → Cachegrind

두 가지 Arm Tarmac 로그 형식의 **PC별 명령어 실행 횟수**를 ELF와 결합해
함수·파일·소스 라인별 **flat Cachegrind 프로파일**로 변환합니다.

Python 표준 라이브러리와 대상 코어용 `objdump`를 사용하며, 소스 위치 조회에는
GNU 호환 `addr2line`을 우선 사용합니다. 자동 탐색으로도 찾지 못하면 objdump의
라인 번호 출력을 파싱합니다.
기본적으로 ELF의 명령어 주소를 `Ir=0`으로 포함하고 trace 실행 횟수를 더합니다.
로그는 한 줄씩 읽고, ELF/trace의 고유 PC별 횟수를 모읍니다. addr2line을 사용하는
경우 주소를 일괄 질의합니다.
전체 로그를 메모리에 올리거나 명령어 실행마다 외부 프로세스를 만들지 않습니다.

출력 이벤트는 `Ir` 하나입니다. **call graph, 호출 횟수, inclusive cost,
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
- 결과 확인용 `cg_annotate`(Valgrind에 포함) 또는 KCachegrind/QCachegrind.

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

cg_annotate --show=Ir --sort=Ir cachegrind.out.core0
cg_annotate --show=Ir --sort=Ir cachegrind.out.core1
```

형식은 기본적으로 자동 인식합니다. 필요하면 `--format es` 또는 `--format it`로
제한할 수 있습니다. 제한한 형식과 다른 명령어 이벤트가 나오면 오류로 처리합니다.

**한 번의 실행에 한 코어의 로그와 대응 ELF를 넣으세요.** 서로 다른 코어/이미지의
로그를 합치면 동일한 PC 주소를 구분할 수 없습니다. 같은 코어의 두 문법을 자동
인식하는 것과 다중 코어를 식별하는 것은 별개입니다. 코어 태그가 추가된 통합 로그는
현재 지원하지 않습니다. 코어별 flat 실행 횟수를 만들 때는 `tic`/`ps` 시간축 정렬이
필요하지 않습니다.

## 여러 시나리오 일괄 변환 및 merge

Linux에서 수백 개 로그를 처리할 때는 **별도의 Python 배치 진입점**을 사용합니다.
Bash에서 변환기를 로그마다 실행하면 같은 ELF를 반복 분석하게 됩니다. 아래 스크립트는
기존 파서를 import하고 ELF 디스어셈블/소스 매핑을 공유합니다. trace에서 새롭게 발견된
PC만 추가 조회하며, 한 번에 하나의 로그만 읽습니다. 병렬 작업 프로세스를 만들지 않습니다.

```bash
python3 tarmac_batch_to_cachegrind.py /work/test_results \
  --elf /work/fw/core0.elf \
  --objdump arm-none-eabi-objdump \
  --output-dir /work/coverage/run001
```

- 상위 폴더 아래를 재귀 탐색합니다. 기본 파일명 패턴은 `tarmac*.log` 및
  `tarmac*.log.gz`이며 대소문자를 구분합니다. 심볼릭 링크는 따라가지 않습니다.
- 먼저 파일명으로 후보를 좁히고 실제 변환 파서로 검증합니다. 모든 `.log`의 내용을
  별도로 탐색하는 이중 읽기를 하지 않습니다. `build.log` 등은 무시하며, 후보 파일에
  Tarmac 명령어/예외 이벤트가 없거나 명령어 형식이 잘못됐으면 실패 목록에 기록합니다.
- 기본 출력 폴더는 `상위폴더/cachegrind-output`입니다. 매 실행마다 **비어 있는 출력
  폴더**를 지정하세요. 이전 실행 결과와 섞거나 덮어쓰지 않습니다.
- 상대 폴더 경로를 `_`로 연결하고 `.log`/`.log.gz`를 제거해 이름을 만듭니다.
  루트 바로 아래의 로그는 상위 폴더 자체의 이름을 붙입니다. 충돌하는 이름이 생기면
  변환 전에 오류로 알리므로 폴더명이나 `--pattern`을 조정하세요.

| 입력(상위폴더 기준) | 출력 파일명 |
|---|---|
| `case01/tarmac_core0.log` | `case01_tarmac_core0_cachegrind.out` |
| `case02/tarmac_core0.log` | `case02_tarmac_core0_cachegrind.out` |
| `group/case03/tarmac_core0.log.gz` | `group_case03_tarmac_core0_cachegrind.out` |

끝에는 **`total_merge_cachegrind.out`**과 **`batch_report.json`**도 생성됩니다.
merge는 변환 중 메모리에서 동일한 `파일·함수·라인`의 `Ir`을 합산합니다. 개별 출력
파일 800개를 다시 읽는 post-merge 단계가 없습니다. 미실행 라인의 0은 유지되며,
어느 시나리오에서든 실행한 라인은 합계가 양수가 됩니다. `Ir`은 실행 횟수의 합계이지
그 라인을 실행한 시나리오의 개수가 아닙니다.

개별 파일이 필요 없고 최종 merge만 보고 싶다면 `--merge-only`를 추가하세요.
개별 프로파일 쓰기를 생략해 디스크 작업을 줄입니다.

```bash
python3 tarmac_batch_to_cachegrind.py /work/test_results \
  --elf /work/fw/core0.elf --pattern 'tarmac_core0*.log' \
  --merge-only -o /work/coverage/core0-merged
```

`--pattern`은 파일 basename에 적용하며 여러 번 지정할 수 있습니다. 지정하면 기본
패턴을 대체합니다. 셸에서 먼저 확장되지 않도록 **따옴표로 감싸세요.** `.log.gz`도
선택하려면 해당 패턴을 추가하세요. `--addr2line`, `--objdump`, `--load-offset`,
`--format`, `--executed-only`도 단일 변환기와 같은 방식으로 사용할 수 있습니다.
addr2line이 자동 탐색되지 않으면 objdump의 라인 정보를 재사용합니다.

### 코어별 ELF가 다를 때

**한 배치 실행은 동일한 ELF와 주소 매핑을 사용하는 로그만 선택해야 합니다.**
두 문법 모두 파싱 가능하다는 것이 두 ELF의 주소 공간을 구분한다는 뜻은 아닙니다.
파일명만으로 ELF가 일치하는지는 검증할 수 없습니다. 각 폴더의 두 로그가 서로 다른
코어/ELF를 사용한다면 패턴과 출력 폴더를 분리해서 두 번 실행하세요.

```bash
python3 tarmac_batch_to_cachegrind.py /work/test_results \
  --elf core0.elf --pattern 'tarmac_core0*.log' -o /work/coverage/core0

python3 tarmac_batch_to_cachegrind.py /work/test_results \
  --elf core1.elf --pattern 'tarmac_core1*.log' -o /work/coverage/core1
```

### 실패 처리

각 파일의 성공/실패 및 통계는 `batch_report.json`에 남습니다. 잘못된 로그가 있어도
나머지를 계속 변환하며, 실패가 하나라도 있으면 종료 코드는 1입니다. 일부만 성공하면
성공한 로그의 결과로 total을 생성하되 파일의 `desc:`에 **PARTIAL MERGE**를 표시합니다.
모두 실패하면 total은 만들지 않습니다. ELF 분석 실패나 출력 이름 충돌 등 공통 오류는
변환 시작 전에 중단합니다. 실행 명령어가 0개라도 EXC/IS 같은 인식 가능한 이벤트가
있으면 기본 coverage 모드에서 0으로 처리하지만, 빈 파일이나 무관한 텍스트는 실패합니다.

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
desc: Tarmac flat instruction profile; no cache simulation
cmd: firmware.elf
events: Ir
fl=src/main.c
fn=main
42 300
43 100
44 0
summary: 400
```

`42 300`은 소스 42번 라인에 대응하는 명령어들의 실행 횟수 합계가 300이라는
뜻입니다. **소스 라인 자체가 300번 실행됐다는 뜻은 아닙니다.** 함수의 비용도
그 함수에 직접 귀속된 명령어 수의 합계이며, 하위 함수의 비용을 더하지 않습니다.
16비트와 32비트 명령어 모두 실행당 1입니다. 명령어 바이트 수나 cycle 수가 아닙니다.

### 미실행 코드와 coverage

기본 동작은 `objdump -d -z --no-show-raw-insn`으로 ELF의 실행 가능한 섹션에서
명령어 시작 주소를 수집하는 것입니다. 각 주소에 0을 설정하고 trace의 횟수를
합산하므로, 한 번도 호출되지 않은 함수나 실행되지 않은 라인도 `44 0`처럼 출력됩니다.
같은 라인의 다른 PC가 실행됐다면 해당 라인의 값은 양수가 됩니다. `summary`에는
실행된 횟수만 합산되므로 0인 항목이 늘어도 총 실행 수는 변하지 않습니다.

메모리의 모든 바이트 주소를 PC로 취급하지 않습니다. 비실행 데이터 섹션은 제외하며,
코드 안에서도 objdump가 `.word`/`.short` 등으로 표시하는 데이터와 해석 불가 항목은
제외합니다. Arm/Thumb 구분과 코드 내 literal pool 식별을 위해 해당 아키텍처를
지원하는 objdump 및 mapping symbol이 보존된 ELF를 사용하는 것이 좋습니다.

**coverage의 범위는 ELF에 남아 있고 소스 라인으로 매핑 가능한 명령어입니다.**
최적화/링커에 의해 제거된 코드, 주석, 빈 줄은 0인 실행 가능 라인으로 추가하지 않습니다.
한 소스 라인에 여러 명령어나 분기가 있으면 일부만 실행돼도 `Ir > 0`이므로,
이 값은 완전한 branch/condition coverage를 나타내지 않습니다.

CSV에도 미실행 PC를 `Ir=0`으로 포함합니다. JSON의 `unique_pcs`는 trace에서 실제로
실행된 고유 PC 수이고, `elf_instruction_pcs`는 objdump에서 수집한 고유 명령어 주소
수입니다. `--load-offset`은 ELF 주소를 runtime 주소로 변환하는 단계에도 적용됩니다.
뷰어가 0인 항목을 숨기면 표시 필터를 조정하세요. `cg_annotate`에서는
`--threshold=0 --show=Ir --sort=Ir`로 확인할 수 있습니다.

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

Python 3.6.8 및 3.12.14 인터프리터에서 아래 35개 테스트의 통과를 확인했습니다.
`dataclasses` 등의 backport 패키지를 설치할 필요는 없습니다.

- 제공된 두 로그 문법을 바탕으로 만든 fixture: 명령어 추출, EXC/메모리 제외,
  조건 실패, folded 명령어, malformed 입력 처리.
- 실제 GCC 생성 ELF + `addr2line`: 함수/라인 연결, 두 문법의 출력 일치,
  offset, gzip/stdin, 미해결 주소 및 비용 보존.
- 미실행 함수의 0 비용, 데이터 제외, 같은 라인의 0/양수 비용 합산, 빈 trace,
  실행 PC 전용 모드 및 잘못된 objdump 지정 시 기존 출력 보존.
- addr2line 없는 PATH에서 실제 objdump fallback: 두 trace 형식, offset, CSV,
  미실행 함수, 실행 PC 전용 모드, 미해결 PC 보존 및 명시적 도구 경로 오류.
- 800개 합성 로그의 일괄 변환/merge 및 ELF 분석 재사용, 재귀 파일명, 패턴 필터,
  부분 실패 보고, 출력 충돌/기존 결과 보호, merge 전용 모드 및 배치 fallback.
- `cg_annotate`가 설치되어 있으면 생성한 프로파일을 실제 reader로 검증합니다.
  전체 검증을 위해 GCC/binutils/Valgrind를 설치한 환경에서 테스트를 실행하세요.

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
