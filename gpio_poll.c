/*
 * gpio_poll.c
 * ===========
 *
 * Program which reads pushbuttons and 2-bit gray code rotors attached to GPIO pins,
 * emitting events to STDOUT when they are pressed / released / rotated. Also supports
 * PCF8574 compatible GPIO expanders.
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <stdarg.h>
#include <unistd.h>
#include <ctype.h>

#include <sys/types.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <sys/epoll.h>
#include <sys/resource.h>
#include <sys/time.h>

#include <linux/i2c-dev.h>
#include <fcntl.h>
#include <errno.h>
#include <time.h>

#include "argparse.h"

#define LONG_PRESS_TIME 50

#define EVT_ROTOR 0x80
#define EVT_UP 0x40
#define EVT_LONG 0x20

#define MIN_PIN 2
#define MAX_PIN 40

#define MAX_EXPANDERS 8

#define GPIO_BASE_PATH "/sys/class/gpio/"
#define GPIO_BIT(v) (1<<(v))

#define PIN(pinstr, v) ((input_pin((pinstr).pin)->val & (pinstr).mask) ? 0 : (v))

typedef struct {
    uint8_t val;
    uint8_t i2c_addr;
    int gpiofd;
} input_pin_t;

typedef struct {
    int pin;
    uint8_t mask;
} pin_t;

typedef struct {
    pin_t pin;
    uint8_t lastval;
    uint8_t debounce;
    uint8_t long_press;
} button_t;

typedef struct {
    pin_t pin1;
    pin_t pin2;
    int8_t lastval;
    int8_t count;
} rotor_t;

static input_pin_t input_pins[MAX_PIN - MIN_PIN + 1];
static int expander_pin[8];
static rotor_t rotors[32];
static button_t buttons[32];
static int numexpanders = 0;
static int numbuttons = 0;
static int numrotors = 0;
static int debug_enable = 0;
static int i2c_bus = 1;

static uint64_t monotime() {
    struct timespec sp;
    clock_gettime(CLOCK_MONOTONIC, &sp);
    return (1000 * (uint64_t)sp.tv_sec) + (sp.tv_nsec / 1000000);
}

static input_pin_t* input_pin(int pin) {
    if (pin < MIN_PIN || pin > MAX_PIN)
        return NULL;
    return &input_pins[pin - MIN_PIN];
}

static int gpio_exists(int num) {
    struct stat st;
    char gpiopath[64];
    snprintf(gpiopath, 64, GPIO_BASE_PATH "gpio%d", num);
    if (stat(gpiopath, &st) < 0) {
        return 0;
    }
    if (!S_ISDIR(st.st_mode)) {
        return 0;
    }
    return 1;
}

static int ensure_gpio(int num) {
    if (!gpio_exists(num)) {
        FILE* f = fopen(GPIO_BASE_PATH "export", "w");
        if (!f) {
            perror("open " GPIO_BASE_PATH "export");
            return 0;
        }
        if (fprintf(f, "%d\n", num) < 0) {
            perror("writing to " GPIO_BASE_PATH "export");
            fclose(f);
            return 0;
        }
        fclose(f);
    }
    if (gpio_exists(num)) {
        return 1;
    }
    fprintf(stderr, "cannot export gpio %d\n", num);
    return 0;
}

static int gpio_dev_write(int num, const char* file, const char* value) {
    char gpiopath[64];
    snprintf(gpiopath, 64, GPIO_BASE_PATH "gpio%d/%s", num, file);
    int fd = open(gpiopath, O_WRONLY);
    if (fd < 0) {
        return 0;
    }
    if (write(fd, value, strlen(value)) < 0) {
        close(fd);
        return 0;
    }
    close(fd);
    return 1;
}

static char skipspace(const char** str) {
    while (isspace(**str)) (*str)++;
    return **str;
}

static int tryparse(const char* val, long* rv, int base) {
    char* end;
    if (!*val) return 0;
    *rv = strtol(val, &end, base);
    if (*end) return 0;
    return 1;
}

static argparse_result_t parse_pin(argparser_t* p, const char* txt, pin_t* pin, const char** end) {
    long epin = strtol(txt, (char**)end, 10);
    input_pin_t* ipin;
    if (*end == txt)
        return arg_error(p, "Invalid pin: %s", txt);

    if (**end == ':') {
        int exp = epin;
        txt = *end + 1;
        epin = strtol(txt, (char**)end, 10);
        if (*end == txt)
            return arg_error(p, "Invalid pin: %s", txt);

        if (exp < 0 || exp >= MAX_EXPANDERS || (pin->pin = expander_pin[exp]) == -1)
            return arg_error(p, "Expander %d not defined", exp);

        ipin = input_pin(pin->pin);

        if (epin < 0 || epin > 7)
            return arg_error(p, "Expander pin %d out of range (0 - 7)", epin);

        pin->mask = (1 << epin);
        if (ipin->val & pin->mask)
            return arg_error(p, "Expander pin %d:%d already used", exp, epin);

        ipin->val |= pin->mask;
    } else {
        pin->pin = epin;
        pin->mask = 1;

        ipin = input_pin(epin);
        if (ipin == NULL)
            return arg_error(p, "Input pin %d out of range", epin);

        if (ipin->val || ipin->i2c_addr) {
            return arg_error(p, "Input pin %d already used", epin);
        }

        // Mark the input pin as used
        ipin->val = 1;
    }

    return ARGPARSE_OK;
}

static argparse_result_t arg_button(argparser_t* p, argument_t* arg, const char* txt) {
    const char* end = NULL;
    int res;
    if (numbuttons >= 32)
        return arg_error(p, "Too many buttons");

    button_t *btn = &buttons[numbuttons++];

    ARGPARSE_CHECK(parse_pin(p, txt, &btn->pin, &end));

    if (*end != 0)
        return arg_error(p, "Invalid button definition: %s", txt);

    btn->lastval = 0;
    return ARGPARSE_OK;
}

static argparse_result_t arg_rotor(argparser_t* p, argument_t* arg, const char* txt) {
    const char* end = NULL;
    int res;

    if (numrotors >= 32)
        return arg_error(p, "Too many rotors");

    rotor_t* rotor = &rotors[numrotors++];

    ARGPARSE_CHECK(parse_pin(p, txt, &rotor->pin1, &end));
    if (skipspace(&end) != ',')
        return arg_error(p, "Must specify two pins for rotor: %s", txt);

    txt = end + 1;
    skipspace(&txt);

    ARGPARSE_CHECK(parse_pin(p, txt, &rotor->pin2, &end));

    if (*end != 0)
        return arg_error(p, "Invalid rotor definition: %s", txt);

    rotor->lastval = 0;
    return ARGPARSE_OK;
}

static argparse_result_t arg_expander(argparser_t* p, argument_t* arg, const char* txt) {
    const char* end;
    input_pin_t* ipin;
    long addr = strtol(txt, (char**)&end, 16);

    if (numexpanders >= MAX_EXPANDERS)
        return arg_error(p, "Too many expanders");

    if (end == txt || skipspace(&end) != ':')
        return arg_error(p, "Invalid expander definition: %s", txt);

    if (!(3 <= addr && addr <= 127))
        return arg_error(p, "I2C address %02x out of range (03-7F)", addr);

    txt = end + 1;
    skipspace(&txt);

    long pin = strtol(txt, (char**)&end, 10);
    if (end == txt || skipspace(&end) != 0)
        return arg_error(p, "Invalid interrupt pin: %s", txt);

    ipin = input_pin(pin);
    if (ipin == NULL)
        return arg_error(p, "Interrupt pin %d out of range (%d - %d)", pin, MIN_PIN, MAX_PIN);

    if (ipin->val || ipin->i2c_addr)
        return arg_error(p, "Interrupt pin %d already used", pin);

    ipin->i2c_addr = addr;

    expander_pin[numexpanders++] = pin;
    return ARGPARSE_OK;
}

static argparse_result_t arg_i2c(argparser_t* p, argument_t* arg, const char* optarg) {
    long bus;

    if (!tryparse(optarg, &bus, 10) || bus < 0 || bus > 255)
        return arg_error(p, "Invalid I2C bus number: %s", optarg);

    i2c_bus = (int)bus;
    return ARGPARSE_OK;
}

static argparse_result_t arg_debug(argparser_t* p, argument_t* arg, const char* optarg) {
    debug_enable = 1;
    return ARGPARSE_OK;
}

argument_t argument_definitions[] = {
    {"config",   'c', &arg_parse_config_file, 1, "Read the specified config file"},
    {"button",   'b', &arg_button,            1, "Define a pushbutton: pin or expander:pin"},
    {"rotor",    'r', &arg_rotor,             1, "Define a rotor: pin_up, pin_down"},
    {"expander", 'e', &arg_expander,          1, "Define an expander: i2c_addr,interrupt_pin"},
    {"debug",    'D', &arg_debug,             0, "Enable debugging"},
    {"i2c",      'i', &arg_i2c,               1, "I2C bus number (default: 1)"},
    {NULL}
};


static void emit_event(uint8_t evt) {
    if (debug_enable) {
        fprintf(stderr, "EVENT: %02x\n", evt);
    } else {
        if (write(1, &evt, 1) < 0) {
            perror("write pipe");
        }
    }
}

static int setup_input_pin(input_pin_t* pin, int gpionum, int epfd, struct epoll_event* event, const char* edge) {
    char gpiopath[64];

    if (!ensure_gpio(gpionum)) {
        fprintf(stderr, "cannot export GPIO %d\n", gpionum);
        return 1;
    }

    if (!gpio_dev_write(gpionum, "direction", "in")) {
        fprintf(stderr, "cannot set GPIO %d to input\n", gpionum);
        return 1;
    }

    if (!gpio_dev_write(gpionum, "edge", edge)) {
        fprintf(stderr, "cannot set GPIO %d edge to %s\n", gpionum, edge);
        return 1;
    }

    snprintf(gpiopath, 64, GPIO_BASE_PATH "gpio%d/value", gpionum);
    pin->gpiofd = open(gpiopath, O_RDONLY);
    if (pin->gpiofd < 0) {
        fprintf(stderr, "cannot open GPIO %d: %s", gpionum, strerror(errno));
        return 1;
    }

    event->events = EPOLLIN | EPOLLET;
    event->data.ptr = pin;
    epoll_ctl(epfd, EPOLL_CTL_ADD, pin->gpiofd, event);

    return 0;
}

static void dump_pin(pin_t* pin) {
    input_pin_t* ipin = input_pin(pin->pin);
    if (ipin->i2c_addr) {
        fprintf(stderr, "Exp 0x%02X:%d, mask %02X", ipin->i2c_addr, pin->pin, pin->mask);
    } else {
        fprintf(stderr, "GPIO %d", pin->pin);
    }
}

int main(int argc, char** argv) {
    int i;
    char gpiopath[64];
    struct epoll_event events[8];
    argparser_t argparse;

    memset(&events, 0, sizeof(events));
    memset(&input_pins, 0, sizeof(input_pins));
    memset(&buttons, 0, sizeof(buttons));
    memset(&rotors, 0, sizeof(rotors));

    for (i = 0; i < MAX_EXPANDERS; i++) {
        expander_pin[i] = -1;
    }

    int epfd = epoll_create(1);
    if (epfd < 0) {
        perror("create epoll");
        return 1;
    }

    arg_init(&argparse, argument_definitions, argc, argv);

    while (argparse.index < argparse.argc) {
        const char* arg = argparse.argv[argparse.index];
        argparse.argdef = NULL;
        argparse.parsed_option[0] = 0;
        int res = arg_parse_argument(&argparse);
        if (res != ARGPARSE_OK) {
            const char* option_name = argparse.argdef != NULL ? argparse.argdef->longopt : argparse.parsed_option;
            if (*option_name != '\0') {
                fprintf(stderr, "Error: Option \"%s\": %s\n\n", option_name, argparse.error);
            } else {
                fprintf(stderr, "Error: Argument \"%s\": %s\n\n", arg, argparse.error);
            }
            arg_print_usage(&argparse, stderr);
            return 1;
        }
    }

    if (numbuttons == 0 && numrotors == 0) {
        arg_print_usage(&argparse, stderr);
        return 1;
    }

    if (debug_enable) {
        for (i = 0; i < numbuttons; i++) {
            button_t* btn = &buttons[i];
            fprintf(stderr, "Button %d: ", i);
            dump_pin(&btn->pin);
            fprintf(stderr, "\n");

        }

        for (i = 0; i < numrotors; i++) {
            rotor_t* rot = &rotors[i];
            fprintf(stderr, "Rotor %d: ", i);
            dump_pin(&rot->pin1);
            fprintf(stderr, " / ");
            dump_pin(&rot->pin2);
            fprintf(stderr, "\n");
        }
    }

    for (i = MIN_PIN; i <= MAX_PIN; i++) {
        input_pin_t* pin = input_pin(i);
        if (pin->val) {
            int res = setup_input_pin(pin, i, epfd, &events[0], pin->i2c_addr ? "falling" : "both");
            if (res != 0)
                return res;
        }
    }

    int i2cfd = 0;
    if (numexpanders) {
        sprintf(gpiopath, "/dev/i2c-%d", i2c_bus);
        i2cfd = open(gpiopath, O_RDWR);
        if (i2cfd < 0) {
            perror("open i2c");
            return 1;
        }
    }

    setpriority(PRIO_PROCESS, 0, -20);

    uint64_t nexttick = monotime() + 5;

    while (1) {
        int i;
        uint64_t ctime = monotime();
        int32_t wtime = nexttick - ctime;
        if (wtime < 0) {
            wtime = 0;
            nexttick += 5;
            if (nexttick <= ctime) {
                nexttick = ctime + 5;
            }

            for (i = 0; i < numbuttons; i++) {
                button_t* btn = &buttons[i];
                if (btn->debounce) {
                    if (!--btn->debounce) {
                        if (btn->lastval) {
                            btn->long_press = LONG_PRESS_TIME;
                            emit_event(i);
                        } else {
                            emit_event(EVT_UP | i | (btn->long_press ? 0 : EVT_LONG));
                            btn->long_press = 0;
                        }
                    }
                }
                if (btn->long_press) {
                    if (!--btn->long_press) {
                        emit_event(EVT_LONG | i);
                    }
                }
            }
        }

        int cnt = epoll_wait(epfd, events, 8, wtime);
        //printf("cnt = %d\n", cnt);
        for (i = 0; i < cnt; i++) {
            struct epoll_event* evt = &events[i];
            input_pin_t* exp = (input_pin_t*)evt->data.ptr;
            if (exp->i2c_addr != 0) {
                if (ioctl(i2cfd, I2C_SLAVE, (int)exp->i2c_addr) < 0) {
                    perror("ioctl I2C_SLAVE");
                    continue;
                }
                int nr = read(i2cfd, &exp->val, 1);
                if (nr < 0) {
                    exp->val = 0xFF;
                    perror("i2c read");
                    continue;
                }
            } else {
                uint8_t data[2];
                int nr = pread(exp->gpiofd, data, 2, 0);
                if (nr < 0) {
                    perror("pread button GPIO");
                }
                if (nr > 1)
                    exp->val = data[0] == '1';
            }
        }
        for (i = 0; i < cnt; i++) {
            uint8_t data[2];
            struct epoll_event* evt = &events[i];
            input_pin_t* exp = (input_pin_t*)evt->data.ptr;
            // read from "value" to reset it
            if (exp->i2c_addr != 0) {
                if (pread(exp->gpiofd, data, 2, 0) < 0) {
                    perror("pread expander GPIO");
                }
            }
        }
        if (cnt) {
            char text[256];
            char* cp = text;
            if (debug_enable) {
                for (i = 0; i < numexpanders; i++) {
                    int j;
                    input_pin_t* exp = input_pin(expander_pin[i]);
                    for (j = 0; j < 8; j++) {
                        *cp++ = (exp->val & (1 << j)) ? '.' : '!';
                    }
                }
                *cp++ = '\n';
                if (write(2, text, cp - text) < 0) {
                    perror("write debug");
                }
            }

            cp = text;
            for (i = 0; i < numrotors; i++) {
                rotor_t* rotor = &rotors[i];
                int8_t val = PIN(rotor->pin1, 0xC0) ^ PIN(rotor->pin2, 0x40);
                int8_t ofs = (val - rotor->lastval);
                if (ofs) {
                    rotor->count += ofs >> 6;
                    //fprintf(stderr, "count = %d\n", rotor->count);
                    if (val == 0) {
                        if (rotor->count == 4) {
                            emit_event(EVT_ROTOR | EVT_UP | i);
                        } else if (rotor->count == -4) {
                            emit_event(EVT_ROTOR | i);
                        }
                        rotor->count = 0;
                    }
                    rotor->lastval = val;
                }
            }
            for (i = 0; i < numbuttons; i++) {
                button_t* btn = &buttons[i];
                int val = PIN(btn->pin, 1);
                if (val != btn->lastval) {
                    btn->debounce = 2;
                    btn->lastval = val;
                }
            }
            if (debug_enable) {
                char* t;
                for (t = text; t < cp; t++) {
                    fprintf(stderr, "  %02x\n", *t);
                }
            } else {
                if (write(1, text, cp - text) < 0) {
                    perror("write pipe");
                }
            }
        }
    }
    return 0;
}
