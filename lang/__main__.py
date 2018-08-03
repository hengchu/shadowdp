import argparse
from pycparser import parse_file, c_generator


def main():
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument('file', metavar='FILE', type=str, nargs=1)
    arg_parser.add_argument('-o', '--out',
                            action='store', dest='out', type=str,
                            help='The output file name.', required=False)
    results = arg_parser.parse_args()
    results.file = results.file[0]
    results.out = results.file[0:results.file.rfind('.')] + '_t.c' if results.out is None else results.out

    ast = parse_file(results.file, use_cpp=True)
    generator = c_generator.CGenerator()
    with open(results.out, 'w') as f:
        f.write(generator.visit(ast))


if __name__ == '__main__':
    main()