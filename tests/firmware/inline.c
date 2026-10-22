/* Inline attribution test: func_B is inlined into func_A under a condition,
 * so func_A's instructions are interleaved with func_B's source lines. */
volatile unsigned short reg;
volatile int sink;

static inline __attribute__((always_inline)) void func_B(int v)
{
    sink = v * 3;
}

int func_A(int x)
{
    if (x > 5)
        func_B(x);
    return reg + x;
}
