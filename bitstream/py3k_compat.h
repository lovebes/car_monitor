
#include <Python.h>

#if PY_MAJOR_VERSION >= 3
#define STRING_Check PyBytes_Check
#define STRING_AsStringAndSize PyBytes_AsStringAndSize
#define STRING_FromStringAndSize PyBytes_FromStringAndSize
#else
#define STRING_Check PyString_Check
#define STRING_AsStringAndSize PyString_AsStringAndSize
#define STRING_FromStringAndSize PyString_FromStringAndSize
#endif
