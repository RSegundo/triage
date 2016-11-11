# -*- coding: utf-8 -*-
from itertools import product, chain
from functools import reduce
import sqlalchemy.sql.expression as ex
from sqlalchemy.ext.compiler import compiles


def make_list(a):
    return [a] if not type(a) in (list, tuple) else list(a)


def make_sql_clause(s, constructor):
    if not isinstance(s, ex.ClauseElement):
        return constructor(s)
    else:
        return s


class CreateTableAs(ex.Executable, ex.ClauseElement):

    def __init__(self, name, query):
        self.name = name
        self.query = query


@compiles(CreateTableAs)
def _create_table_as(element, compiler, **kw):
    return "CREATE TABLE %s AS %s" % (
        element.name,
        compiler.process(element.query)
    )


def to_sql_name(name):
    return name.replace('"', '')


class Aggregate(object):
    """
    An object representing one or more SQL aggregate columns in a groupby
    """
    def __init__(self, quantity, function, name=None):
        """
        Args:
            quantity: an SQL string expression for the quantity to aggregate
            function: an SQL aggregate function
            name: a name for the quantity, used in the aggregate column name

        Note that quantity and function can also be collections of the above,
        in which case the cross product of those is used. If quantity is a
        collection than name should also be a collection of the same length.
        """
        self.quantities = make_list(quantity)
        self.functions = make_list(function)

        if name is not None:
            self.quantity_names = make_list(name)
            if len(self.quantity_names) != len(self.quantities):
                raise ValueError("Name length doesn't match quantity length")
        else:
            self.quantity_names = [x.replace('"', '') for x in self.quantities]

    def get_columns(self, when=None, prefix=None):
        """
        Args:
            when: used in a case statement to filter the rows going into the
                aggregation function
            prefix: prefix for column names
        Returns:
            collection of SQLAlchemy columns
        """
        if prefix is None:
            prefix = ""

        name_template = "{prefix}{quantity_name}_{function}"
        if when is None:
            column_template = "{function}({quantity})"
        else:
            column_template = ("{function}(CASE WHEN {when} "
                               "THEN {quantity} END)")

        format_kwargs = dict(prefix=prefix, when=when)

        for function, (quantity, quantity_name) in product(
                self.functions, zip(self.quantities, self.quantity_names)):
            format_kwargs.update(quantity=quantity, function=function,
                                 quantity_name=quantity_name)
            column = column_template.format(**format_kwargs)
            name = name_template.format(**format_kwargs)

            yield ex.literal_column(column).label(to_sql_name(name))


class SpacetimeAggregation(object):
    def __init__(self, aggregates, group_intervals, from_obj, dates,
                 prefix=None, suffix=None, date_column=None):
        """
        Args:
            aggregates: collection of Aggregate objects
            from_obj: defines the from clause, e.g. the name of the table
            group_intervals: a dictionary of group : intervals pairs where
                group is an expression by which to group and
                intervals is a collection of datetime intervals, e.g.
                {"address_id": ["1 month", "1 year]}
            dates: list of PostgreSQL date strings,
                e.g. ["2012-01-01", "2013-01-01"]
            prefix: prefix for column names, defaults to from_obj
            suffix: suffix for aggregation table, defaults to "aggregation"
            date_column: name of date column in from_obj, defaults to "date"

        The from_obj and group arguments are passed directly to the
            SQLAlchemy Select object so could be anything supported there.
            For details see:
            http://docs.sqlalchemy.org/en/latest/core/selectable.html
        """
        self.aggregates = aggregates
        self.from_obj = make_sql_clause(from_obj, ex.table)
        self.group_intervals = group_intervals
        self.groups = group_intervals.keys()
        self.dates = dates
        self.prefix = prefix if prefix else str(from_obj)
        self.suffix = suffix if suffix else "aggregation"
        self.date_column = date_column if date_column else "date"

    def _get_aggregates_sql(self, interval, date, group):
        """
        Helper for getting aggregates sql
        Args:
            interval: SQL time interval string, or "all"
            date: SQL date string
            group: group clause, for naming columns
        Returns: collection of aggregate column SQL strings
        """
        if interval != 'all':
            when = "'{date}' <= {date_column} + interval '{interval}'".format(
                    interval=interval, date=date, date_column=self.date_column)
        else:
            when = None

        prefix = "{prefix}_{group}_{interval}_".format(
                prefix=self.prefix, interval=interval.replace(' ', ''),
                group=group)

        return chain(*(a.get_columns(when, prefix) for a in self.aggregates))

    def get_selects(self):
        """
        Constructs select queries for this aggregation

        Returns: a dictionary of group : queries pairs where
            group are the same keys as group_intervals
            queries is a list of Select queries, one for each date in dates
        """
        queries = {}

        for group, intervals in self.group_intervals.items():
            queries[group] = []
            for date in self.dates:
                columns = [group,
                           ex.literal_column("'%s'::date"
                                             % date).label("date")]
                columns += list(chain(*(self._get_aggregates_sql(
                        i, date, group) for i in intervals)))

                where = ex.text("{date_column} < '{date}'".format(
                        date_column=self.date_column, date=date))

                gb_clause = make_sql_clause(group, ex.literal_column)
                queries[group].append(
                        ex.select(columns=columns, from_obj=self.from_obj)
                          .where(where)
                          .group_by(gb_clause))

        return queries

    def _get_table_name(self, group):
        """
        Returns name for table for the given group
        """
        return '"%s"' % to_sql_name("%s_%s" % (self.prefix, group))

    def get_creates(self, selects=None):
        """
        Construct create queries for this aggregation
        Args:
            selects: the dictionary of select queries to use
                if None, use self.get_selects()
                this allows you to customize select queries before creation

        Returns:
            a dictionary of group : create pairs where
                group are the same keys as group_intervals
                create is a CreateTableAs object
        """
        if not selects:
            selects = self.get_selects()

        selects = {group: reduce(lambda s, t: s.union_all(t), sels)
                   for group, sels in selects.items()}

        return {group: CreateTableAs(self._get_table_name(group), select)
                for group, select in selects.items()}

    def get_drops(self):
        """
        Generate drop queries for this aggregation

        Returns: a dictionary of group : drop pairs where
            group are the same keys as group_intervals
            drop is a raw drop table query for the corresponding table
        """
        return {group: "DROP TABLE IF EXISTS %s;" % self._get_table_name(group)
                for group in self.groups}

    def get_indexes(self):
        """
        Generate create index queries for this aggregation

        Returns: a dictionary of group : index pairs where
            group are the same keys as group_intervals
            index is a raw create index query for the corresponding table
        """
        return {group: "CREATE INDEX ON %s (%s, %s);" %
                (self._get_table_name(group), group, "date")
                for group in self.groups}

    def get_join_table(self):
        """
        Generate a query for a join table
        """
        return ex.Select(columns=self.groups, from_obj=self.from_obj)\
                 .group_by(*self.groups)

    def get_create(self, join_table=None):
        """
        Generate a single aggregation table creation query by joining
            together the results of get_creates()
        Returns: a CREATE TABLE AS query
        """
        if not join_table:
            join_table = '(%s) t1' % self.get_join_table()

        name = "%s_%s" % (self.prefix, self.suffix)

        query = ("SELECT * FROM %s\n"
                 "CROSS JOIN (select unnest('{%s}'::date[]) as date) t2\n") % (
                join_table, str.join(',', self.dates))
        for group in self.groups:
            query += "LEFT JOIN %s USING (%s, date)" % (
                    self._get_table_name(group), group)

        return "CREATE TABLE %s AS (%s);" % (name, query)

    def get_drop(self):
        """
        Generate a drop table statement for the aggregation table
        Returns: string sql query
        """
        name = "%s_%s" % (self.prefix, self.suffix)
        return "DROP TABLE IF EXISTS %s" % name
