import argparse
from lang.core import LangTransformer
from pycparser import parse_file
import coloredlogs

coloredlogs.install(level='DEBUG', fmt='%(levelname)s:%(module)s: %(message)s')

__HEADER = \
    r"""extern void __VERIFIER_error() __attribute__ ((__noreturn__));
    extern int __VERIFIER_nondet_float(void);
    extern void __VERIFIER_assume(int);
    extern int __VERIFIER_nondet_int();
    extern void __assert_fail();
    #define __VERIFIER_assert(cond) { if(!(cond)) { __assert_fail(); } }
    #define abs(x) ((x) < 0 ? -(x) : (x))
    typedef enum { false = 0, true = 1 } bool;
    
    """


def main():
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument('file', metavar='FILE', type=str, nargs=1)
    arg_parser.add_argument('-o', '--out',
                            action='store', dest='out', type=str,
                            help='The output file name.', required=False)
    results = arg_parser.parse_args()
    results.file = results.file[0]
    results.out = results.file[0:results.file.rfind('.')] + '_t.c' if results.out is None else results.out

    ast = parse_file(results.file, use_cpp=True, cpp_path='gcc', cpp_args=['-E'])
    transformer = LangTransformer()

    with open(results.out, 'w') as f:
        # write verifier headers
        f.write(__HEADER)
        f.write(transformer.visit(ast))


if __name__ == '__main__':
    main()
