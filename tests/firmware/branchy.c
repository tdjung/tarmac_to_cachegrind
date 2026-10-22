/* Test firmware for branch coverage: each function exercises one branch shape.
 * Deliberately leaves some outcomes uncovered (see expected comments). */
#include <stdint.h>

#define NOINLINE __attribute__((noinline))

volatile uint32_t sink;
volatile uint32_t ticks;
static uint8_t buf[16];
static uint32_t len;

NOINLINE int clamp(int v, int lo, int hi)      /* if/else-if chain */
{
    if (v < lo)
        return lo;
    if (v > hi)
        return hi;
    return v;
}

NOINLINE int both(int a, int b)                /* short-circuit && and || */
{
    if (a > 0 && b > 0)
        return 1;
    if (a < -100 || b < -100)                  /* never true below */
        return 2;
    return 0;
}

NOINLINE int first_nonzero(const uint8_t *p, int n) /* cbz/cbnz-style loop */
{
    for (int i = 0; i < n; i++)
        if (p[i])
            return i;
    return -1;
}

NOINLINE int classify(unsigned k)              /* switch */
{
    switch (k) {
    case 0: return 10;
    case 1: return 20;
    case 2: return 30;
    case 7: return 70;                         /* never hit */
    default: return -1;
    }
}

NOINLINE void push(uint8_t v)                  /* never-called function */
{
    if (len < sizeof buf)
        buf[len++] = v;
    else
        sink = 0xdead;
}

NOINLINE int maybe_call(int x)                 /* conditional call */
{
    if (x == 3)
        push((uint8_t)x);
    return x + 1;
}

int asm_cbz(int); int asm_cbnz(int); int asm_ret_eq(int);
int asm_pop_ne(int); int asm_call_gt(int);

void SysTick_Handler(void)
{
    ticks++;
}

static void semihost_exit(int code)
{
    /* SYS_EXIT_EXTENDED: works on both M4 (v7-M) and M33 (v8-M) under QEMU. */
    uint32_t block[2] = {0x20026u, (uint32_t)code};
    register uint32_t r0 __asm__("r0") = 0x20;
    register uint32_t *r1 __asm__("r1") = block;
    __asm__ volatile("bkpt 0xab" : : "r"(r0), "r"(r1) : "memory");
    for (;;) {}
}

int main(void)
{
    /* SysTick every 97 cycles so interrupts land between branches. */
    *(volatile uint32_t *)0xE000E014 = 97;
    *(volatile uint32_t *)0xE000E018 = 0;
    *(volatile uint32_t *)0xE000E010 = 7;

    uint8_t data[8] = {0, 0, 0, 5, 0, 0, 0, 0};
    int acc = 0;
    for (int i = -3; i < 12; i++) {
        acc += clamp(i, 0, 9);
        acc += both(i, 4 - i);
        acc += classify((unsigned)i & 3);
        acc += maybe_call(i);
    }
    acc += first_nonzero(data, 8);
    for (int i = 0; i < 3; i++) {
        acc += asm_cbz(i) + asm_ret_eq(i) + asm_call_gt(i);
        acc += asm_cbnz(0);              /* never non-zero: one side only */
        acc += asm_pop_ne(i + 5);        /* i+5 == 5 once */
    }
    acc += first_nonzero(data, 3);
    sink = (uint32_t)acc;
    *(volatile uint32_t *)0xE000E010 = 0;
    semihost_exit(0);
    return 0;
}
