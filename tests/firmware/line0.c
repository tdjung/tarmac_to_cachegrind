/* Line-0 fixture: clang gives some instructions no source line (DWARF line
 * 0), including inside B's inlined body. The converter must place them on a
 * real line (see attribute_line_zero) instead of dropping them from it. */
volatile int sink;
volatile int out;
int tbl[16];

static int B(int v, int w)
{
    int s = 0;
    for (int i = 0; i < v; i++)
        s += tbl[(i + w) & 15] * 3;
    if (s > w) sink = s; else sink = w;
    return s ^ v;
}

int A(int x, int y)
{
    int a = x * 7 + y;
    int b = B(x, a);
    if (b > 3) b = B(y, b);
    int c = tbl[(y + b) & 15];
    return a + b + c;
}

void line0_start(void)
{
    out = A(3, 4) + A(9, 1);
    for (;;)
        ;
}
