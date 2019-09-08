"""Routines for managing SQL statements"""
from pglast import Node, parse_sql
from pglast.node import Scalar

from splitgraph.exceptions import UnsupportedSQLError


def _validate_range_var(node):
    if "schemaname" in node.attribute_names:
        raise UnsupportedSQLError("Table names must not be schema-qualified!")


# Whitelist of permitted AST nodes. When crawling the parse tree, a node not in this list fails validation. If a node
# is in this list, the crawler continues down the tree.
_IMPORT_SQL_PERMITTED_NODES = [
    "RawStmt",
    "SelectStmt",
    "ResTarget",
    "ColumnRef",
    "A_Star",
    "String",
    "A_Expr",
    "A_Const",
    "Integer",
    "JoinExpr",
    "SortBy",
    "NullTest",
    "BoolExpr",
    "CoalesceExpr",
    "RangeFunction",
    "TypeCast",
    "TypeName",
    "SubLink",
    "WithClause",
    "CommonTableExpr",
    "A_ArrayExpr",
    "Float",
]

_SPLITFILE_SQL_PERMITTED_NODES = _IMPORT_SQL_PERMITTED_NODES + [
    "InsertStmt",
    "UpdateStmt",
    "DeleteStmt",
    "CreateStmt",
    "CreateTableAsStmt",
    "IntoClause",
    "AlterTableStmt",
    "AlterTableCmd",
    "DropStmt",
    "ColumnDef",
    "Constraint",
]

# Nodes in this list have extra validators that are supposed to return None or raise an Exception if they
# fail validation.
_SQL_VALIDATORS = {"RangeVar": _validate_range_var}


def _validate_node(node, permitted_nodes, node_validators):
    if isinstance(node, Scalar):
        return
    node_class = node.node_tag
    if node_class in node_validators:
        node_validators[node_class](node)
    elif node_class not in permitted_nodes:
        message = "Unsupported statement type %s" % node_class
        if isinstance(node["location"], Scalar):
            message += " near character %d" % node["location"].value
        raise UnsupportedSQLError(message + "!")


def validate_splitfile_sql(sql):
    """
    Check an SQL query to see if it can be safely used in a Splitfile SQL command. The rules for usage are:

      * Only basic DDL (CREATE/ALTER/DROP table) and DML (SELECT/INSERT/UPDATE/DELETE) are permitted.
      * All tables must be non-schema-qualified (the statement is run with `search_path` set to the single
        schema that a Splitgraph image is checked out into).
      * Function invocations are forbidden.

    :param sql: SQL query
    :return: None if validation is successful
    :raises: UnsupportedSQLException if validation failed
    """
    tree = Node(parse_sql(sql))
    for node in tree.traverse():
        _validate_node(
            node, permitted_nodes=_SPLITFILE_SQL_PERMITTED_NODES, node_validators=_SQL_VALIDATORS
        )


def validate_import_sql(sql):
    """
    Check an SQL query to see if it can be safely used in an IMPORT statement
    (e.g. `FROM noaa/climate:latest IMPORT {SELECT * FROM rainfall WHERE state = 'AZ'} AS rainfall`.
    In this case, only a single SELECT statement is supported.

    :param sql: SQL query
    :return: None if validation is successful
    :raises: UnsupportedSQLException if validation failed
    """

    tree = Node(parse_sql(sql))
    if len(tree) != 1:
        raise UnsupportedSQLError("The query is supposed to consist of only one SELECT statement!")

    for node in tree.traverse():
        _validate_node(
            node, permitted_nodes=_IMPORT_SQL_PERMITTED_NODES, node_validators=_SQL_VALIDATORS
        )