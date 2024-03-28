#include <stdarg.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>

#include "argparse.h"

argparse_result_t arg_error(argparser_t* p, const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(p->error, sizeof(p->error), fmt, ap);
    va_end(ap);
    return ARGPARSE_ERR;
}

void arg_init(argparser_t* p, argument_t* definitions, int argc, char** argv) {
    p->definitions = definitions;
    p->argc = argc;
    p->argv = argv;
    p->index = 1;
    p->argdef = NULL;
    p->parsed_option[0] = 0;
    p->error[0] = 0;
}
static argument_t* find_argument_short(argparser_t* p, char shortarg) {
    for (argument_t* curarg = p->definitions; curarg->longopt; curarg++) {
        if (curarg->shortopt == shortarg)
            return curarg;
    }
    return NULL;
}

static argument_t* find_argument_long(argparser_t* p, char* longarg) {
    for (argument_t* curarg = p->definitions; curarg->longopt; curarg++) {
        if (strcmp(longarg, curarg->longopt) == 0)
            return curarg;
    }
    return NULL;
}

static int _parse_one_argument(argparser_t* p, argument_t* argdef, char* optarg) {
    if (argdef == NULL)
        return arg_error(p, "Unknown option");

    if (argdef->hasarg) {
        if (optarg == NULL) {
            if (p->index >= p->argc)
                return arg_error(p, "Option requires an argument");

            optarg = p->argv[p->index++];
        }
    } else {
        if (optarg != NULL)
            return arg_error(p, "Option does not take an argument");
    }

    return argdef->parse(p, argdef, optarg);
}

argparse_result_t arg_parse_argument(argparser_t* p) {
    int res;
    const char* arg = p->argv[p->index++];
    if (arg[0] == '-') {
        if (arg[1] == '-') {
            arg += 2; // skip "--"

            char* eq_pos = strchr(arg + 2, '=');
            char* optarg;

            if (eq_pos != NULL) {
                int arglen = eq_pos - arg;
                if (arglen >= sizeof(p->parsed_option)) arglen = sizeof(p->parsed_option) - 1;
                strncpy(p->parsed_option, arg, arglen);
                p->parsed_option[arglen] = 0;
                optarg = eq_pos + 1;
            } else {
                strncpy(p->parsed_option, arg, sizeof(p->parsed_option));
                p->parsed_option[sizeof(p->parsed_option) - 1] = 0;
                optarg = NULL;
            }

            argument_t* argdef = find_argument_long(p, p->parsed_option);
            p->argdef = argdef;

            return _parse_one_argument(p, argdef, optarg);
        } else if (arg[1] != 0) {
            // Single dash
            p->parsed_option[1] = 0;
            for (const char* shortarg = arg + 1; *shortarg; shortarg++) {
                p->parsed_option[0] = *shortarg;
                argument_t* argdef = find_argument_short(p, *shortarg);
                p->argdef = argdef;

                ARGPARSE_CHECK(_parse_one_argument(p, argdef, NULL));
            }
            return ARGPARSE_OK;
        }
    }
    return arg_error(p, "Invalid argument");
}

void arg_print_usage(argparser_t* p, FILE* stream) {
    fprintf(stream, "Usage: %s [options]\n", p->argv[0]);
    fprintf(stream, "\n");
    for (argument_t* curarg = p->definitions; curarg->longopt; curarg++) {
        int cpos = 0;
        cpos += fprintf(stream, "  ");
        if (curarg->shortopt)
            cpos += fprintf(stream, "-%c, ", curarg->shortopt);
        cpos += fprintf(stream, "--%s", curarg->longopt);

        while (cpos < 30) {
            cpos++;
            fputc(' ', stream);
        }
        fprintf(stream, " %s\n", curarg->help);
    }
    fprintf(stream, "\n");
}

argparse_result_t arg_parse_config_file(argparser_t* p, argument_t* unused, const char* path) {
    char linebuf[128];
    FILE* config = fopen(path, "r");
    if (!config) {
        return arg_error(p, "Cannot open %s: %s", path, strerror(errno));
    }
    while (fgets(linebuf, 128, config)) {
        char* line = linebuf;
        char* end = linebuf + strlen(linebuf);

        char* key;
        char* value;
        while (end > linebuf && isspace(end[-1])) {
            *--end = 0;
        }
        while (isspace(*line)) line++;

        if (*line == '#' || *line == 0)
            continue;

        key = line;
        value = key;
        while (*value != 0 && !isspace(*value))
            value++;

        if (*value != 0)
            *value++ = 0;
        while (isspace(*value)) value++;

        strncpy(p->parsed_option, key, sizeof(p->parsed_option));

        argument_t* argdef = find_argument_long(p, key);
        p->argdef = argdef;

        if (argdef == NULL) {
            fclose(config);
            return arg_error(p, "Unknown option");
        }
        if (argdef->hasarg) {
            if (!*value) {
                fclose(config);
                return arg_error(p, "Option requires an argument");
            }
        } else {
            if (*value) {
                fclose(config);
                return arg_error(p, "Option does not an argument");
            }
        }
        int res = argdef->parse(p, argdef, *value ? value : NULL);
        if (res != ARGPARSE_OK) {
            fclose(config);
            return res;
        }
    }
    fclose(config);
    return ARGPARSE_OK;
}
