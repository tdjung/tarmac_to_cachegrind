# Tarmac → Cachegrind

두 가지 Arm Tarmac 로그 형식의 **PC별 명령어 실행 횟수**를 ELF와 결합해
함수·파일·소스 라인별 **flat Cachegrind 프로파일**로 변환합니다.

Python 표준 라이브러리와 GNU 호환 `addr2line`만 사용합니다.
로그는 한 줄씩 읽고, 고유 PC별 횟수를 모은 후 `addr2line`에 일괄 질의합니다.
전체 로그를 메모리에 올리거나 명령어 실행마다 외부 프로세스를 만들지 않습니다.

출력 이벤트는 `Ir` 하나입니다. **call graph, 호출 횟수, inclusive cost,
실행 시간, 캐시 hit/miss는 추정하지 않습니다.** Interrupt handler의 명령어도
해당 PC의 함수/소스에 직접 귀속되므로 호출 스택 복원이 필요하지 않습니다.

## 요구사항

- Python 3.6.8 이상. `pip install` 불필요.
- 해당 ELF를 읽을 수 있는 `addr2line`:
  `arm-none-eabi-addr2line`, `llvm-addr2line`, `addr2line` 순으로 자동 탐색합니다.
  여러 toolchain이 있으면 `--addr2line`으로 직접 지정하세요.
- 실행 이미지에 대응하는 ELF. 함수명에는 심볼, 파일/라인에는 DWARF 디버그 정보가
  필요합니다. 실제 사용한 최적화 옵션을 유지하고 `-g`를 포함한 ELF를 사용하세요.
- 결과 확인용 `cg_annotate`(Valgrind에 포함) 또는 KCachegrind/QCachegrind.

## 빠른 시작: 두 코어를 각각 변환

```bash
python3 tarmac_to_cachegrind.py core0.log \
  --elf core0.elf \
  --addr2line arm-none-eabi-addr2line \
  -o cachegrind.out.core0

python3 tarmac_to_cachegrind.py core1.log \
  --elf core1.elf \
  --addr2line arm-none-eabi-addr2line \
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
명령어 이벤트 외의 줄은 무시합니다. 실행 명령어가 하나도 없으면 실패합니다.
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
summary: 400
```

`42 300`은 소스 42번 라인에 대응하는 명령어들의 실행 횟수 합계가 300이라는
뜻입니다. **소스 라인 자체가 300번 실행됐다는 뜻은 아닙니다.** 함수의 비용도
그 함수에 직접 귀속된 명령어 수의 합계이며, 하위 함수의 비용을 더하지 않습니다.
16비트와 32비트 명령어 모두 실행당 1입니다. 명령어 바이트 수나 cycle 수가 아닙니다.

미해결 파일/함수는 `???`, 미해결 라인은 `0`으로 기록해 비용을 보존합니다.
디버그 정보가 없는 ELF라도 함수 심볼이 있으면 함수별 집계가 가능할 수 있습니다.
심볼이나 소스 라인을 찾지 못한 실행 수는 stderr 및 선택적 JSON에 보고합니다.
PC가 ELF 범위를 벗어나도 해당 비용을 버리지 않습니다.

Inlining은 `addr2line -f -C`의 단일 결과에 한 번만 귀속합니다(`-i` 미사용).
인라인 호출 계층을 만들지 않습니다. 최적화로 인한 라인 매핑의 모호함, 심볼 alias,
별도 이미지/overlay, self-modifying code를 복원하지 않습니다.

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

Python 3.6.8 및 3.12.14 인터프리터에서 아래 20개 테스트의 통과를 확인했습니다.
`dataclasses` 등의 backport 패키지를 설치할 필요는 없습니다.

- 제공된 두 로그 문법을 바탕으로 만든 fixture: 명령어 추출, EXC/메모리 제외,
  조건 실패, folded 명령어, malformed 입력 처리.
- 실제 GCC 생성 ELF + `addr2line`: 함수/라인 연결, 두 문법의 출력 일치,
  offset, gzip/stdin, 미해결 주소 및 비용 보존.
- `cg_annotate`가 설치되어 있으면 생성한 프로파일을 실제 reader로 검증합니다.
  전체 검증을 위해 GCC/binutils/Valgrind를 설치한 환경에서 테스트를 실행하세요.

ELF 통합 테스트에는 `gcc`, `nm`, `addr2line`이 필요하며 없으면 해당 테스트가
skip됩니다. 테스트는 호스트에서 컴파일한 ELF 주소에 합성 Tarmac 이벤트를 연결하는
방식입니다. 실제 Arm 코어에서 수집한 전체 trace와 사용자 ELF의 검증을 대신하지는
않습니다. `tests/fixtures`의 로그는 짧은 파서 검증용 예시이며 대응 펌웨어 ELF를
포함하지 않습니다.

## 참고 문서

- [Arm Tarmac 이벤트 및 형식 설명](https://github.com/ARM-software/tarmac-trace-utilities/blob/main/doc/index.rst)
- [Cachegrind 출력 형식](https://valgrind.org/docs/manual/cg-manual.html#cg-manual.impl-details.file-format)
- [GNU addr2line](https://sourceware.org/binutils/docs/binutils/addr2line.html)
