# MIT License
#
# Copyright (c) 2018-2019 Yuxin (Ryan) Wang
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import sympy as sp
import logging
import copy
import re
from pycparser import c_ast
from pycparser.c_generator import CGenerator
from pycparser.c_ast import NodeVisitor
from shadowdp.typesystem import TypeSystem, convert_to_ast, is_node_equal
from shadowdp.exceptions import *
logger = logging.getLogger(__name__)

_code_generator = CGenerator()


class _ExpressionFinder(NodeVisitor):
    """ this class find a specific node in the expression"""
    def __init__(self, check_func, ignores=None):
        self._check_func = check_func
        self._ignores = ignores
        self._nodes = []

    def visit(self, node):
        super().visit(node)
        return self._nodes

    def generic_visit(self, node):
        if self._ignores and not self._ignores(node):
            return
        if self._check_func(node):
            self._nodes.append(node)
        for child in node:
            self.generic_visit(child)


class _ShadowBranchGenerator(NodeVisitor):
    """ this class generates the shadow branch statement"""
    def __init__(self, shadow_variables, types):
        """
        :param shadow_variables: the variable list whose shadow distances should be updated
        """
        self._shadow_variables = shadow_variables
        self._expression_replacer = _ExpressionReplacer(types, False)

    def visit_Decl(self, node):
        raise NotImplementedError('currently doesn\'t support declaration in branch')

    def visit_Compound(self, node):
        # TODO: currently doesn't support ArrayRef
        # only generate shadow execution for dynamically tracked variables
        node.block_items = [child for child in node.block_items
                            if isinstance(child, c_ast.Assignment) and child.lvalue.name in self._shadow_variables]
        for child in node:
            if isinstance(child, c_ast.Assignment):
                child.rvalue = c_ast.BinaryOp(op='-', left=self._expression_replacer.visit(child.rvalue),
                                              right=c_ast.ID(name=child.lvalue.name))
                # change the assignment variable name to shadow distance variable
                child.lvalue.name = '__SHADOWDP_SHADOW_DISTANCE_{}'.format(child.lvalue.name)
            else:
                self.visit(child)


class _ExpressionReplacer(NodeVisitor):
    """ this class returns the aligned or shadow version of an expression, e.g., returns e^aligned or e^shadow of e"""
    def __init__(self, types, is_aligned):
        assert isinstance(types, TypeSystem)
        self._types = types
        self._is_aligned = is_aligned

    def _replace(self, node):
        if not isinstance(node, (c_ast.ArrayRef, c_ast.ID)):
            raise NotImplementedError('Expression type {} currently not supported.'.format(type(node)))
        varname = node.name.name if isinstance(node, c_ast.ArrayRef) else node.name
        alignd, shadow = self._types.get_distance(varname)
        distance = alignd if self._is_aligned else shadow
        if distance == '0':
            return node
        elif distance == '*':
            distance_varname = '__SHADOWDP_{}_DISTANCE_{}'.format('ALIGNED' if self._is_aligned else 'SHADOW', varname)
            distance_var = c_ast.ArrayRef(name=c_ast.ID(name=distance_varname), subscript=node.subscript) \
                if isinstance(node, c_ast.ArrayRef) else c_ast.ID(name=distance_varname)
            return c_ast.BinaryOp(op='+', left=node, right=distance_var)
        else:
            return c_ast.BinaryOp(op='+', left=node, right=convert_to_ast(distance))

    def visit_BinaryOp(self, node):
        if isinstance(node.left, (c_ast.ArrayRef, c_ast.ID)):
            node.left = self._replace(node.left)
        else:
            self.visit(node.left)

        if isinstance(node.right, (c_ast.ArrayRef, c_ast.ID)):
            node.right = self._replace(node.right)
        else:
            self.visit(node.right)

    def visit_UnaryOp(self, node):
        if isinstance(node.expr, (c_ast.ArrayRef, c_ast.ID)):
            node.expr = self._replace(node.expr)
        else:
            self.visit(node.expr)

    def visit(self, node):
        super().visit(node)
        return node


class _ExpressionSimplifier(NodeVisitor):
    """ this class simplifes Ternary operations, e.g., e?c1:c2 + e?c3:c4 -> e?(c1+c2):(c3+c4) """
    def visit_BinaryOp(self, n):
        if isinstance(n.left, c_ast.TernaryOp) and isinstance(n.right, c_ast.TernaryOp) and is_node_equal(n.left.cond, n.right.cond):
            return c_ast.TernaryOp(cond=n.left.cond,
                                   iftrue=c_ast.BinaryOp(op=n.op, left=n.left.iftrue, right=n.right.iftrue),
                                   iffalse=c_ast.BinaryOp(op=n.op, left=n.left.iffalse, right=n.right.iffalse))
        return n

    def visit_TernaryOp(self, n):
        # TODO
        return n

    def visit_UnaryOp(self, n):
        # TODO
        return n


class _DistanceGenerator(NodeVisitor):
    def __init__(self, types):
        self._types = types
        assert isinstance(self._types, TypeSystem)

    def try_simplify(self, expr):
        from sympy import simplify
        try:
            expr = str(simplify(expr))
        finally:
            return expr

    def generic_visit(self, node):
        raise NotImplementedError

    def visit_Constant(self, n):
        return '0', '0'

    def visit_ID(self, n):
        align, shadow = self._types.get_distance(n.name)
        align = '(__SHADOWDP_ALIGNED_DISTANCE_{0})'.format(n.name) if align == '*' else align
        shadow = '(__SHADOWDP_SHADOW_DISTANCE_{0})'.format(n.name) if shadow == '*' else shadow
        return align, shadow

    def visit_ArrayRef(self, n):
        varname, subscript = n.name.name, _code_generator.visit(n.subscript)
        align, shadow = self._types.get_distance(n.name.name)
        align = '(__SHADOWDP_ALIGNED_DISTANCE_{0}[{1}])'.format(varname, subscript) if align == '*' else align
        shadow = '(__SHADOWDP_SHADOW_DISTANCE_{0}[{1}])'.format(varname, subscript) if shadow == '*' else shadow
        return align, shadow

    def visit_BinaryOp(self, n):
        return [self.try_simplify('{} {} {}'.format(left, n.op, right))
                for left, right in zip(self.visit(n.left), self.visit(n.right))]


class ShadowDPTransformer(NodeVisitor):
    """ Traverse the AST and do necessary transformations on the AST according to the typing rules."""
    def __init__(self, function_map=None, set_epsilon=None, set_goal=None):
        """ Initialize the transformer.
        :param function_map: A dict containing a mapping from logical commands (assert / assume / havoc)
        to actual commands (e.g., __VERIFIER_assert in CPAChecker), this is an abstraction for use with other
        verification tools that may have other names for assert / assume / havoc commands.
        :param set_epsilon: boolean value indicating if we want to set epsilon to 1 to overcome the non-linearity issue.
        :param set_goal: number indicating the goal to verify the algorithm, e.g., 2 means to verify 2 * epsilon-DP.
        """
        super().__init__()

        # set default value for function_map
        if not function_map:
            self._func_map = {
                'assert': 'assert',
                'assume': 'assume',
                'havoc': 'havoc',
            }
        else:
            if not isinstance(function_map, dict):
                raise ValueError
            if not ('assert' in function_map and 'assume' in function_map and 'havoc' in function_map):
                raise ValueError
            self._func_map = function_map

        self._set_epsilon = set_epsilon
        self._set_goal = set_goal
        self._types = TypeSystem()
        self._parameters = []
        self._random_variables = set()
        # indicator that all at most one record can differ or records can differ
        self._one_differ = True
        # we keep tracks of the parent of each node since pycparser doesn't provide this feature, this is useful
        # for easy trace back
        self._parents = {}
        # indicate if level of loop statements, this is needed since in While statement we might loop until convergence,
        # before convergence we shouldn't do transformation
        self._loop_level = 0
        # this is needed if we add some statements next to the current statement
        # e.g. float eta = havoc(); _v_epsilon = ...;
        # we shouldn't visit the `_v_epsilon = ...;` statement node, so we keep track of the inserted statements
        # to avoid them
        self._inserted = set()
        # pc corresponds to the pc value in paper, which means if the shadow execution diverges or not, and controls
        # the generation of shadow branch
        self._pc = False
        # to track the inserted assume functions so that we don't have to insert redundent assumes
        self._inserted_query_assumes = [[]]

    def _update_pc(self, pc, types, condition):
        # TODO: use Z3 to solve constraints to decide this value
        star_variable_finder = _ExpressionFinder(
            lambda node: (isinstance(node, c_ast.ID) and
                          ('__SHADOWDP_' in types.get_distance(node.name)[1] or
                           types.get_distance(node.name)[1] == '*')))
        return pc or len(star_variable_finder.visit(condition)) != 0

    # Instrumentation rule
    def _instrument(self, types1, types2, pc):
        if not isinstance(types1, TypeSystem) or not isinstance(types2, TypeSystem):
            raise ValueError('types1 and types2 must be TypeSystem')

        query_var_checker = _ExpressionFinder(
            lambda node: isinstance(node, c_ast.ArrayRef) and '__SHADOWDP_' in node.name.name and
                         self._parameters[2] in node.name.name)

        assumes, inserted_statement = [], []
        for name, distances1 in types1.variables():
            if name not in types2:
                continue

            distances2 = types2.get_distance(name)
            for type_index in range(2):
                version = 'ALIGNED' if type_index == 0 else 'SHADOW'
                if distances1[type_index] != '*' and distances2[type_index] == '*':
                    for query in query_var_checker.visit(convert_to_ast(distances1[type_index])):
                        assumes.extend(self._assume_query(query))
                        self._inserted_query_assumes[-1].append(query)
                    if type_index == 0 or (type_index == 1 and not pc):
                        inserted_statement.append(c_ast.Assignment(
                            op='=', lvalue=c_ast.ID('__SHADOWDP_{}_DISTANCE_{}'.format(version, name)),
                            rvalue=convert_to_ast(distances1[type_index])))

        return assumes + inserted_statement

    def _assume_query(self, query_node):
        """ instrument assume functions of query input (sensitivity guarantee) """
        assume_functions = []
        shadow_distance_node = copy.deepcopy(query_node)
        align_distance_node = copy.deepcopy(query_node)
        regex = re.compile(r'__SHADOWDP_[A-Z]+_DISTANCE_([_a-zA-Z][a-zA-Z0-9\[\]]*)')
        align_distance_node.name.name = regex.sub(r'__SHADOWDP_ALIGNED_DISTANCE_\g<1>', query_node.name.name)
        shadow_distance_node.name.name = regex.sub(r'__SHADOWDP_SHADOW_DISTANCE_\g<1>', query_node.name.name)
        common_assume = [
                c_ast.FuncCall(
                    name=c_ast.ID(self._func_map['assume']),
                    args=c_ast.ExprList(exprs=[c_ast.BinaryOp(op='<=',
                                                              left=align_distance_node,
                                                              right=c_ast.Constant('int', '1'))])),
                c_ast.FuncCall(
                    name=c_ast.ID(self._func_map['assume']),
                    args=c_ast.ExprList(exprs=[c_ast.BinaryOp(op='>=',
                                                              left=align_distance_node,
                                                              right=c_ast.Constant('int', '-1'))])),
                c_ast.FuncCall(
                    name=c_ast.ID(self._func_map['assume']),
                    args=c_ast.ExprList(exprs=[c_ast.BinaryOp(op='==',
                                                              left=shadow_distance_node,
                                                              right=align_distance_node)]))
            ]
        # insert following statements:
        # if (i == __SHADOWDP_index) {
        #   assume(__SHADOWDP_ALIGNED_DISTANCE_q[i] >= -1); assume(__SHADOWDP_ALIGNED_DISTANCE_q[i] <= 1);
        #   assume(__SHADOWDP_SHADOW_DISTANCE_q[i] == __SHADOWDP_ALIGNED_DISTANCE_q[i]);
        # }
        # else {
        #   assume(__SHADOWDP_ALIGNED_DISTANCE_q[i] == 0);
        #   assume(__SHADOWDP_SHADOW_DISTANCE_q[i] == __SHADOWDP_ALIGNED_DISTANCE_q[i]);
        # }
        if self._one_differ:
            if_block = c_ast.If(cond=c_ast.BinaryOp('==',
                                                    left=query_node.subscript,
                                                    right=c_ast.ID(name='__SHADOWDP_index')),
                                iftrue=c_ast.Compound(block_items=[]),
                                iffalse=c_ast.Compound(block_items=[]))
            if_block.iftrue.block_items = common_assume
            if_block.iffalse.block_items = [
                c_ast.FuncCall(
                    name=c_ast.ID(self._func_map['assume']),
                    args=c_ast.ExprList(exprs=[c_ast.BinaryOp(op='==',
                                                              left=shadow_distance_node,
                                                              right=align_distance_node)])),
                c_ast.FuncCall(
                    name=c_ast.ID(self._func_map['assume']),
                    args=c_ast.ExprList(exprs=[c_ast.BinaryOp(op='==',
                                                              left=align_distance_node,
                                                              right=c_ast.Constant('int', '0'))]))

            ]
            assume_functions.append(if_block)
        # insert following statements:
        # assume(__SHADOWDP_ALIGNED_DISTANCE_q[i] >= -1); assume(__SHADOWDP_ALIGNED_DISTANCE_q[i] <= 1);
        # assume(__SHADOWDP_SHADOW_DISTANCE_q[i] == __SHADOWDP_ALIGNED_DISTANCE_q[i]);
        else:
            assume_functions = common_assume
        # if assume function has already been inserted in this scope
        for inserted in self._inserted_query_assumes[-1]:
            if is_node_equal(assume_functions, inserted):
                return []
        self._inserted_query_assumes[-1].append(assume_functions)
        return assume_functions

    def visit(self, node):
        if node in self._inserted:
            # ignore the inserted statement
            return
        for child in node:
            self._parents[child] = node
        return super().visit(node)

    def visit_FuncDef(self, node):
        # the start of the transformation
        self._types.clear()
        logger.info('Start transforming function {} ...'.format(node.decl.name))

        # first pickup the annotation for parameters
        assume_statement, type_statement = node.body.block_items.pop(0), node.body.block_items.pop(0)
        if not all((isinstance(assume_statement, c_ast.Constant),
                   assume_statement.type == 'string',
                   isinstance(type_statement, c_ast.Constant),
                   type_statement.type == 'string')):
            raise NoParameterAnnotationError(assume_statement.coord)
        sensitivity, *other_assumes = assume_statement.value[1:-1].strip().split(';')
        if sensitivity not in ('ALL_DIFFER', 'ONE_DIFFER'):
            raise ValueError('Annotation for sensitivity should be either \'ALL_DIFFER\' or \'ONE_DIFFER\'')

        # get distances from annotation string and store to type system
        parameter_distances = type_statement.value[1:-1].strip().split(';')
        for parameter in parameter_distances:
            results = re.findall(r'([a-zA-Z_]+):\s*<([*a-zA-Z0-9\[\]]+),\s*([*a-zA-Z0-9\[\]]+)>', parameter)
            if len(results) == 0:
                raise ValueError('Illegal annotation for parameter: {}'.format(parameter))
            else:
                name, align, shadow = results[0]
                if align != shadow:
                    raise ValueError('Annotated distances must be identical. {}'.format(parameter))
                else:
                    self._types.update_distance(name, align, shadow)

        self._one_differ = True if sensitivity == 'ONE_DIFFER' else False

        # visit children
        self.generic_visit(node)

        # get the names of parameters
        epsilon, size, q, *_ = self._parameters

        insert_statements = [
            # insert assume(epsilon > 0)
            c_ast.FuncCall(c_ast.ID(self._func_map['assume']),
                           args=c_ast.ExprList([c_ast.BinaryOp('>', c_ast.ID(epsilon),
                                                               c_ast.Constant('int', 0))])),
            # insert assume(size > 0)
            c_ast.FuncCall(c_ast.ID(self._func_map['assume']),
                           args=c_ast.ExprList([c_ast.BinaryOp('>', c_ast.ID(size),
                                                               c_ast.Constant('int', 0))])),
        ]

        # add user-defined assume
        regex = re.compile(r'assume\(([\sa-zA-Z+\-*\\0-9_><=&|]+)\)')
        for assume in other_assumes:
            if 'assume' in assume:
                for expression in regex.findall(assume):
                    insert_statements.append(c_ast.FuncCall(c_ast.ID(self._func_map['assume']),
                                                            args=convert_to_ast(expression)))

        insert_statements.append(
            # insert float __SHADOWDP_v_epsilon = 0;
            c_ast.Decl(name='__SHADOWDP_v_epsilon',
                       type=c_ast.TypeDecl(declname='__SHADOWDP_v_epsilon',
                                           type=c_ast.IdentifierType(names=['float']),
                                           quals=[]),
                       init=c_ast.Constant('int', '0'),
                       quals=[], funcspec=[], bitsize=[], storage=[])
        )

        # setup different sensitivity settings
        if self._one_differ:
            insert_statements.append(
                # insert assume(__SHADOWDP_index >= 0);
                c_ast.FuncCall(c_ast.ID(self._func_map['assume']),
                               args=c_ast.ExprList([c_ast.BinaryOp('>=', c_ast.ID('__SHADOWDP_index'),
                                                                   c_ast.Constant('int', 0))])),
            )
            insert_statements.append(
                # insert assume(__SHADOWDP_index < size);
                c_ast.FuncCall(c_ast.ID(self._func_map['assume']),
                               args=c_ast.ExprList([c_ast.BinaryOp('<', c_ast.ID('__SHADOWDP_index'),
                                                                   c_ast.ID(size))]))
            )
            # insert parameter __SHADOWDP_index
            node.decl.type.args.params.append(
                c_ast.Decl(name='__SHADOWDP_index',
                           type=c_ast.TypeDecl(declname='__SHADOWDP_index',
                                               type=c_ast.IdentifierType(names=['int']),
                                               quals=[]),
                           init=None,
                           quals=[], funcspec=[], bitsize=[], storage=[])
            )

        # add declarations / new parameters for dynamically tracked variables
        for name, distances in self._types.variables():
            for index, distance in enumerate(distances):
                version = 'ALIGNED' if index == 0 else 'SHADOW'
                if distance == '*' or distance == '__SHADOWDP_{}_DISTANCE_{}'.format(version, name):
                    # if it is a dynamically tracked local variable, add declarations
                    if name not in self._parameters:
                        varname = '__SHADOWDP_{}_DISTANCE_{}'.format(version, name)
                        insert_statements.append(
                            c_ast.Decl(name=varname,
                                       type=c_ast.TypeDecl(declname=varname,
                                                           type=c_ast.IdentifierType(names=['float']), quals=[]),
                                       init=c_ast.Constant('int', '0'), quals=[], funcspec=[], bitsize=[], storage=[]))
                    # if it is a dynamically tracked parameter, add new parameters
                    else:
                        # TODO: should be able to detect the type of parameters
                        if name != q:
                            raise NotImplementedError('Currently only supports * types for query variables')
                        varname = '__SHADOWDP_{}_DISTANCE_{}'.format(version, q)
                        node.decl.type.args.params.append(
                            c_ast.Decl(name=varname,
                                       type=c_ast.ArrayDecl(
                                           type=c_ast.TypeDecl(declname=varname, type=c_ast.IdentifierType(
                                               names=['float']), quals=[]), dim=None, dim_quals=[]),
                                       init=None,
                                       quals=[], funcspec=[], bitsize=[], storage=[])
                        )

        # prepend the inserted statements
        node.body.block_items[:0] = insert_statements

    def visit_Assignment(self, node):
        logger.debug('Line {}: {}'.format(str(node.coord.line), _code_generator.visit(node)))
        varname = node.lvalue.name if isinstance(node.lvalue, c_ast.ID) else node.lvalue.name.name
        if self._loop_level == 0 and self._pc:
            # generate x^shadow = x + x^shadow - e according to (T-Asgn)
            parent = self._parents[node]
            if isinstance(parent, c_ast.Compound):
                node_index = parent.block_items.index(node)
                if isinstance(node.lvalue, c_ast.ID):
                    shadow_distance = c_ast.ID(name='__SHADOWDP_SHADOW_DISTANCE_{}'.format(varname))
                elif isinstance(node.lvalue, c_ast.ArrayRef):
                    shadow_distance = c_ast.ArrayRef(name='__SHADOWDP_SHADOW_DISTANCE_{}'.format(varname),
                                                     subscript=node.lvalue.subscript)
                else:
                    raise NotImplementedError('Assigned value type not supported {}'.format(type(node.lvalue)))
                # insert x^shadow = x + x^shadow - e;
                insert_node = c_ast.Assignment(op='=', lvalue=shadow_distance, rvalue=c_ast.BinaryOp(
                    op='-', left=c_ast.BinaryOp(op='+', left=node.lvalue, right=shadow_distance), right=node.rvalue))
                parent.block_items.insert(node_index, insert_node)
                self._inserted.add(insert_node)
                self._inserted.add(node)
            else:
                raise NotImplementedError('Parent of assignment node not supported {}'.format(type(parent)))

        # check the distance dependence
        dependence_finder = _ExpressionFinder(
            lambda to_check: (isinstance(to_check, c_ast.ID) and to_check.name == varname) or
                             (isinstance(to_check, c_ast.ArrayRef) and to_check.name.name == varname),
            lambda to_ignore: isinstance(to_ignore, c_ast.ArrayRef) and to_ignore.name.name == self._parameters[2]
        )
        for name, distances in self._types.variables():
            if name not in self._random_variables:
                for distance in distances:
                    if distance != '*':
                        if len(dependence_finder.visit(convert_to_ast(distance))) != 0:
                            raise DistanceDependenceError(node.coord, varname, distance)

        # get new distance from the assignment expression (T-Asgn)
        aligned, shadow = _DistanceGenerator(self._types).visit(node.rvalue)
        if self._pc:
            self._types.update_distance(node.lvalue.name, aligned, '*')
        else:
            self._types.update_distance(node.lvalue.name, aligned, shadow)
        logger.debug('types: {}'.format(self._types))

    def visit_Decl(self, node):
        logger.debug('Line {}: {}'.format(str(node.coord.line), _code_generator.visit(node)))

        # if declarations are in function parameters, the distances are already stored in type system due to annotation
        if isinstance(node.type, c_ast.FuncDecl):
            for param_index, decl in enumerate(node.type.args.params):
                self._parameters.append(decl.name)
                if decl.name not in self._types:
                    raise ValueError('Parameter {} not annotated.'.format(decl.name))

            logger.debug('Params: {}'.format(self._parameters))

        # if declarations are in function body, store distance into type system
        elif isinstance(node.type, c_ast.TypeDecl):
            # if no initial value is given, default to (0, 0)
            if not node.init:
                self._types.update_distance(node.name, '0', '0')
            # else update the distance to the distance of initial value (T-Asgn)
            elif isinstance(node.init, (c_ast.Constant, c_ast.BinaryOp, c_ast.BinaryOp, c_ast.UnaryOp)):
                aligned, shadow = _DistanceGenerator(self._types).visit(node.init)
                if self._pc:
                    self._types.update_distance(node.name, aligned, '*')
                else:
                    self._types.update_distance(node.name, aligned, shadow)
            # if it is random variable declaration (T-Laplace)
            elif isinstance(node.init, c_ast.FuncCall) and node.init.name.name == 'Lap':
                if self._pc:
                    raise SamplingCommandMisplaceError(node.coord)
                self._random_variables.add(node.name)
                logger.debug('Random variables: {}'.format(self._random_variables))
                if not (isinstance(node.init.args.exprs[1], c_ast.Constant) and
                        node.init.args.exprs[1].type == 'string'):
                    raise NoSamplingAnnotationError(node.coord)

                # get the annotation for sampling command
                selector, distance_eta, *_ = map(lambda x: x.strip(), node.init.args.exprs[1].value[1:-1].split(';'))
                # set the random variable distance
                # replace the distance variables in annotation with the current distance
                # e.g., replace __SHADOWDP_ALIGNED_DISTANCE_sum with 0 if Gamma = {sum: <0, ->}
                regex = re.compile(r'(__SHADOWDP_(ALIGNED|SHADOW)_DISTANCE_([_a-zA-Z][_a-zA-Z0-9]*)([\[_a-zA-Z0-9\]]*))')
                for distance_var, version, varname, subscript in regex.findall(distance_eta):
                    if varname == self._parameters[2]:
                        continue
                    align, shadow = self._types.get_distance(varname)
                    distance = align if version == 'ALIGNED' else shadow
                    if distance == '*':
                        continue
                    distance_eta = distance_eta.replace(distance_var, distance)

                self._types.update_distance(node.name, distance_eta, '0')

                # update distances of normal variables according to the selector
                for name, (align, shadow) in self._types.variables():
                    # first unwrap the star variables
                    align = '(__SHADOWDP_ALIGNED_DISTANCE_{0})'.format(name) if align == '*' else align
                    shadow = '(__SHADOWDP_SHADOW_DISTANCE_{0})'.format(name) if shadow == '*' else shadow
                    # if the aligned distance and shadow distance are the same
                    # then there's no need to update the distances
                    if align != shadow and name not in self._random_variables and name not in self._parameters:
                        self._types.update_distance(
                            name,
                            selector.replace('SHADOW', '({})'.format(shadow)).replace('ALIGNED', '({})'.format(align)),
                            shadow)

                if self._loop_level == 0:
                    # insert cost variable update statement and transform sampling command to havoc command
                    assert isinstance(self._parents[node], c_ast.Compound)
                    n_index = self._parents[node].block_items.index(node)
                    scale = _code_generator.visit(node.init.args.exprs[0])
                    # incorporate epsilon = 1 approach
                    if self._set_epsilon:
                        epsilon, *_ = self._parameters
                        scale = scale.replace(epsilon, self._set_epsilon)

                    # TODO: maybe create a specialized simplifier for this scenario
                    # transform distance expression to cost expression,
                    # e.g., q[i] + eta > bq ? 2 : 0 -> q[i] + eta > bq ? 2 * 1 / scale : 0
                    pieces = re.split('([?:])', distance_eta)
                    transformed = []
                    for piece in pieces:
                        if len(re.findall(r'[=><\\|&?:]', piece)) == 0:
                            cost_expr = '(Abs({}) * (1/({})))'.format(piece, scale)\
                                .replace('[', '__LEFTBRACE__').replace(']', '__RIGHTBRACE__')
                            cost = str(sp.simplify(cost_expr))
                            cost = cost.replace('__LEFTBRACE__', '[').replace('__RIGHTBRACE__', ']')
                            transformed.append(cost)
                        else:
                            transformed.append(piece)

                    # calculate v_epsilon by combining normal cost and sampling cost
                    v_epsilon = '({}) + ({})'.format(
                        selector.replace('SHADOW', '0').replace('ALIGNED', '__SHADOWDP_v_epsilon'),
                        ''.join(transformed))

                    v_epsilon = _ExpressionSimplifier().visit(convert_to_ast(v_epsilon))
                    update_v_epsilon = c_ast.Assignment(op='=',
                                                        lvalue=c_ast.ID('__SHADOWDP_v_epsilon'), rvalue=v_epsilon)
                    # insert assume functions on query variable if cost variable calculation contains it
                    query_var_checker = _ExpressionFinder(
                        lambda node: isinstance(node, c_ast.ArrayRef) and '__SHADOWDP_' in node.name.name and
                                     self._parameters[2] in node.name.name)

                    self._parents[node].block_items.insert(n_index + 1, update_v_epsilon)
                    for query_node in query_var_checker.visit(update_v_epsilon):
                        assume_functions = self._assume_query(query_node)
                        self._parents[node].block_items[n_index + 1:n_index + 1] = assume_functions
                        for function in assume_functions:
                            self._inserted.add(function)

                    self._inserted.add(update_v_epsilon)

                    # transform sampling command to havoc command
                    node.init = c_ast.FuncCall(c_ast.ID(self._func_map['havoc']), args=None)
            else:
                raise NotImplementedError('Initial value currently not supported: {}'.format(node.init))

        elif isinstance(node.type, c_ast.ArrayDecl):
            # put array variable declaration into type dict
            raise NotImplementedError('Array declaration current not supported')
        else:
            raise NotImplementedError('Declaration statement currently not supported: {}'.format(node))

        logger.debug('types: {}'.format(self._types))

    def visit_If(self, n):
        logger.debug('types(before branch): {}'.format(self._types))
        logger.debug('Line {}: if({})'.format(n.coord.line, _code_generator.visit(n.cond)))

        # update pc value updPC
        before_pc = self._pc
        self._pc = self._update_pc(self._pc, self._types, n.cond)

        before_types = self._types.copy()
        # corresponds to \Gamma_0 in paper
        types_0 = self._types.copy()
        # promote the shadow distances of the assigned variables to *
        shadow_finder = _ExpressionFinder(lambda node: isinstance(node, c_ast.Assignment) and node.lvalue)
        for assign_node in shadow_finder.visit(n):
            if isinstance(assign_node.lvalue, c_ast.ID):
                varname = assign_node.lvalue.name
            elif isinstance(assign_node.lvalue, c_ast.ArrayRef):
                varname = assign_node.lvalue.name.name
            else:
                raise NotImplementedError(
                    'Assign node lvalue type not supported {}'.format(type(assign_node.lvalue)))
            align, shadow = types_0.get_distance(varname)
            types_0.update_distance(varname, align, '*')

        if self._pc and not before_pc:
            self._types = types_0

        # backup the current types before entering the true or false branch
        cur_types = self._types.copy()

        self._inserted_query_assumes.append([])
        # add current condition for simplification
        self._types.apply(n.cond, True)
        # to be used in if branch transformation assert(e^aligned);
        aligned_true_cond = _ExpressionReplacer(self._types, True).visit(
            copy.deepcopy(n.cond))
        self.visit(n.iftrue)
        true_types = self._types
        logger.debug('types(true branch): {}'.format(true_types))
        true_assumes = self._inserted_query_assumes.pop()

        self._inserted_query_assumes.append([])
        # revert current types back to enter the false branch
        self._types = cur_types
        self._types.apply(n.cond, False)
        if n.iffalse:
            logger.debug('Line: {} else'.format(n.iffalse.coord.line))
            self.visit(n.iffalse)
        # to be used in else branch transformation assert(not (e^aligned));
        aligned_false_cond = _ExpressionReplacer(self._types, True).visit(copy.deepcopy(n.cond))
        logger.debug('types(false branch): {}'.format(self._types))
        false_types = self._types.copy()
        self._types.merge(true_types)
        logger.debug('types(after merge): {}'.format(self._types))
        false_assumes = self._inserted_query_assumes.pop()

        exp_checker = _ExpressionFinder(
            lambda node: isinstance(node, c_ast.ArrayRef) and '__SHADOWDP_' in node.name.name and
                         self._parameters[2] in node.name.name)

        if self._loop_level == 0:
            if self._pc and not before_pc:
                # insert c_s
                c_s = self._instrument(before_types, types_0, before_pc)
                if_index = self._parents[n].block_items.index(n)
                self._parents[n].block_items[if_index:if_index] = c_s
                for statement in c_s:
                    self._inserted.add(statement)
                self._inserted.add(n)
                # insert c_shadow
                shadow_cond = _ExpressionReplacer(types_0, False).visit(
                    copy.deepcopy(n.cond))
                shadow_branch = c_ast.If(cond=shadow_cond,
                                         iftrue=c_ast.Compound(
                                             block_items=copy.deepcopy(n.iftrue.block_items)),
                                         iffalse=c_ast.Compound(
                                             block_items=copy.deepcopy(n.iffalse.block_items)) if n.iffalse else None)
                shadow_branch_generator = _ShadowBranchGenerator(
                    {name for name, (_, shadow) in types_0.variables() if shadow == '*'},
                    types_0)
                shadow_branch_generator.visit(shadow_branch)
                if_index = self._parents[n].block_items.index(n)
                self._parents[n].block_items.insert(if_index + 1, shadow_branch)
                self._inserted.add(shadow_branch)

                # insert assume functions before the shadow branch
                for query_node in exp_checker.visit(shadow_cond):
                    assume_functions = self._assume_query(query_node)
                    index = self._parents[n].block_items.index(n) + 1
                    self._parents[n].block_items[index:index] = assume_functions
                    for assume_function in assume_functions:
                        self._inserted.add(assume_function)

            # create else branch if doesn't exist
            n.iffalse = n.iffalse if n.iffalse else c_ast.Compound(block_items=[])

            # insert assert and assume functions to corresponding branch
            for aligned_cond in (aligned_true_cond, aligned_false_cond):
                block_node = n.iftrue if aligned_cond is aligned_true_cond else n.iffalse
                # insert the assertion
                assert_body = c_ast.ExprList(exprs=[aligned_cond]) if aligned_cond is aligned_true_cond else \
                    c_ast.UnaryOp(op='!', expr=c_ast.ExprList(exprs=[aligned_cond]))

                block_node.block_items.insert(0, c_ast.FuncCall(name=c_ast.ID(self._func_map['assert']),
                                                                args=assert_body))
                # if the expression contains `query` variable,
                # add assume functions on __SHADOWDP_ALIGNED_DISTANCE_query and __SHADOWDP_SHADOW_DISTANCE_query
                inserted = true_assumes if aligned_cond is aligned_true_cond else false_assumes
                self._inserted_query_assumes.append(inserted)
                for query_node in exp_checker.visit(aligned_cond):
                    assume_functions = self._assume_query(query_node)
                    block_node.block_items[0:0] = assume_functions
                self._inserted_query_assumes.pop()

            # instrument statements for updating aligned or shadow distance variables (Instrumentation rule)
            for types in (true_types, false_types):
                block_node = n.iftrue if types is true_types else n.iffalse
                inserted = true_assumes if types is true_types else false_assumes
                self._inserted_query_assumes.append(inserted)
                instruments = self._instrument(types, self._types, self._pc)
                block_node.block_items.extend(instruments)
                for instrument in instruments:
                    self._inserted.add(instrument)
                self._inserted_query_assumes.pop()

        self._pc = before_pc

    def visit_While(self, node):
        before_pc = self._pc
        self._pc = self._update_pc(self._pc, self._types, node.cond)

        before_types = self._types.copy()

        fixed_types = None
        # don't output logs while doing iterations
        #logger.disabled = True
        self._loop_level += 1
        while fixed_types != self._types:
            fixed_types = self._types.copy()
            self.generic_visit(node)
            self._types.merge(fixed_types)
        logger.disabled = False
        self._loop_level -= 1

        if self._loop_level == 0:
            self._inserted_query_assumes.append([])
            logger.debug('Line {}: while({})'.format(node.coord.line, _code_generator.visit(node.cond)))
            logger.debug('types(fixed point): {}'.format(self._types))
            aligned_cond = _ExpressionReplacer(self._types, True).visit(
                copy.deepcopy(node.cond))
            assertion = c_ast.FuncCall(name=c_ast.ID(self._func_map['assert']),
                                       args=c_ast.ExprList(exprs=[aligned_cond]))
            self._inserted.add(assertion)
            node.stmt.block_items.insert(0, assertion)
            self.generic_visit(node)
            after_visit = self._types.copy()
            self._types = before_types.copy()
            self._types.merge(fixed_types)

            c_s = self._instrument(before_types, self._types, self._pc)
            while_index = self._parents[node].block_items.index(node)
            self._parents[node].block_items[while_index:while_index] = c_s
            for statement in c_s:
                self._inserted.add(statement)
            self._inserted.add(node)

            update_statements = self._instrument(after_visit, self._types, self._pc)
            node.stmt.block_items.extend(update_statements)
            for statement in update_statements:
                self._inserted.add(statement)

            # TODO: while shadow branch
            if self._pc and not before_pc:
                pass
            self._inserted_query_assumes.pop()

        self._pc = before_pc

    def visit_Return(self, node):
        align, _ = _DistanceGenerator(self._types).visit(node.expr)
        if align != '0':
            raise ReturnDistanceNotZero(node.coord, _code_generator.visit(node.expr), align)

        # insert assert(__SHADOWDP_v_epsilon <= epsilon);
        epsilon, *_ = self._parameters
        if self._set_epsilon and self._set_epsilon.isdigit():
            epsilon_node = c_ast.Constant(type='float', value=self._set_epsilon)
        elif self._set_epsilon and not self._set_epsilon.isdigit():
            epsilon_node = c_ast.ID(name=self._set_epsilon)
        else:
            epsilon_node = c_ast.ID(epsilon)

        if self._set_goal:
            assert_node = c_ast.FuncCall(
                c_ast.ID(self._func_map['assert']), args=c_ast.ExprList(
                    [c_ast.BinaryOp('<=', c_ast.ID(name='__SHADOWDP_v_epsilon'),
                                    c_ast.BinaryOp(op='*', left=epsilon_node, right=convert_to_ast(self._set_goal)))]))
        else:
            assert_node = c_ast.FuncCall(c_ast.ID(self._func_map['assert']),
                                         args=c_ast.ExprList([c_ast.BinaryOp('<=', c_ast.ID('__SHADOWDP_v_epsilon'),
                                                                             epsilon_node)]))
        self._parents[node].block_items.insert(self._parents[node].block_items.index(node), assert_node)
        self._inserted.add(assert_node)
        # because we have inserted a statement before Return statement while iterating, it will be a forever loop
        # add the current node to the set to not visit this same node again
        self._inserted.add(node)
