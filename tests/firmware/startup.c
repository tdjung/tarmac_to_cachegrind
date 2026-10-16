#include <stdint.h>
extern int main(void);
extern void SysTick_Handler(void);
extern uint32_t __stack_top, __data_start, __data_end, __data_load, __bss_start, __bss_end;

void Reset_Handler(void)
{
    uint32_t *src = &__data_load, *dst = &__data_start;
    while (dst < &__data_end) *dst++ = *src++;
    for (dst = &__bss_start; dst < &__bss_end;) *dst++ = 0;
    main();
    for (;;) {}
}

static void Default_Handler(void) { for (;;) {} }

__attribute__((section(".vectors"), used))
const void *vectors[16] = {
    &__stack_top, Reset_Handler, Default_Handler, Default_Handler,
    Default_Handler, Default_Handler, Default_Handler, 0, 0, 0, 0,
    Default_Handler, Default_Handler, 0, Default_Handler, SysTick_Handler,
};
