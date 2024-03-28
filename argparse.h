
#ifndef _ARGPARSE_H_
#define _ARGPARSE_H_

#include <stdio.h>

#define ARGPARSE_CHECK(call) if ((res = (call)) != ARGPARSE_OK) return res

typedef enum {
    ARGPARSE_OK = 0,
    ARGPARSE_ERR = 1
} argparse_result_t;

typedef struct argparser argparser_t;
typedef struct argument argument_t;

struct argument {
    const char* longopt;
    char shortopt;
    argparse_result_t (*parse)(argparser_t*, argument_t*, const char*);
    int hasarg;
    const char* help;
    const char* extrastr;
    long extraint;
    void* extraptr;
};

struct argparser {
    argument_t* definitions;
    char** argv;
    int argc;
    int index;
    struct argument* argdef;
    char error[256];
    char parsed_option[64];
};

extern void arg_init(argparser_t* p, argument_t* definitions, int argc, char** argv);
extern argparse_result_t arg_error(argparser_t* p, const char* fmt, ...);
extern argparse_result_t arg_parse_argument(argparser_t* p);
extern argparse_result_t arg_parse_config_file(argparser_t*, argument_t*, const char*);
extern void arg_print_usage(argparser_t* p, FILE* stream);



#endif
