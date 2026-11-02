"""How a plan written as text nests: PostgreSQL's EXPLAIN and MySQL's EXPLAIN ANALYZE."""

from tracepanel.core import explain as plans

POSTGRES_PLAN = [
    "Sort  (cost=95.43..95.68 rows=100 width=72)",
    "  Sort Key: a.name",
    "  ->  Hash Join  (cost=48.25..92.11 rows=100 width=72)",
    "        Hash Cond: (b.author_id = a.id)",
    "        ->  Nested Loop  (cost=4.47..47.06 rows=100 width=44)",
    "              ->  Bitmap Heap Scan on books b  (cost=4.18..12.64 rows=10 width=40)",
    "                    Recheck Cond: (year > 2000)",
    "                    ->  Bitmap Index Scan on books_year_idx  (cost=0.00..4.18 rows=10 width=0)",
    "                          Index Cond: (year > 2000)",
    "              ->  Index Scan using reviews_book_id_idx on reviews r  (cost=0.29..3.43 rows=1 width=12)",
    "                    Index Cond: (book_id = b.id)",
    "        ->  Hash  (cost=22.00..22.00 rows=1200 width=36)",
    "              ->  Seq Scan on authors a  (cost=0.00..22.00 rows=1200 width=36)",
    "                    Filter: active",
]

MYSQL_TREE = (
    "-> Nested loop inner join  (cost=1.6 rows=2) (actual time=0.05..0.07 rows=2 loops=1)\n"
    "    -> Filter: (b.`year` > 2000)  (cost=0.45 rows=1) (actual time=0.03..0.04 rows=2 loops=1)\n"
    "        -> Table scan on b  (cost=0.45 rows=2) (actual time=0.03..0.03 rows=2 loops=1)\n"
    "    -> Index lookup on r using book_id (book_id=b.id)  (cost=0.35 rows=1) (actual time=0.01..0.01 rows=1 loops=2)"
)


def test_a_postgresql_plan_nests_each_node_and_its_details() -> None:
    rows: plans.Rows = [(line,) for line in POSTGRES_PLAN]

    plan = plans.explain(
        "SELECT * FROM books", None, vendor="postgresql", alias="default", execute=lambda *_: (rows, ["QUERY PLAN"])
    )

    assert plan.to_dict() == {
        "vendor": "postgresql",
        "alias": "default",
        "analyzed": False,
        "lines": [
            {"depth": 0, "text": "Sort  (cost=95.43..95.68 rows=100 width=72)", "warning": None},
            {"depth": 1, "text": "Sort Key: a.name", "warning": None},
            {"depth": 1, "text": "Hash Join  (cost=48.25..92.11 rows=100 width=72)", "warning": None},
            {"depth": 2, "text": "Hash Cond: (b.author_id = a.id)", "warning": None},
            {"depth": 2, "text": "Nested Loop  (cost=4.47..47.06 rows=100 width=44)", "warning": None},
            {"depth": 3, "text": "Bitmap Heap Scan on books b  (cost=4.18..12.64 rows=10 width=40)", "warning": None},
            {"depth": 4, "text": "Recheck Cond: (year > 2000)", "warning": None},
            {
                "depth": 4,
                "text": "Bitmap Index Scan on books_year_idx  (cost=0.00..4.18 rows=10 width=0)",
                "warning": None,
            },
            {"depth": 5, "text": "Index Cond: (year > 2000)", "warning": None},
            {
                "depth": 3,
                "text": "Index Scan using reviews_book_id_idx on reviews r  (cost=0.29..3.43 rows=1 width=12)",
                "warning": None,
            },
            {"depth": 4, "text": "Index Cond: (book_id = b.id)", "warning": None},
            {"depth": 2, "text": "Hash  (cost=22.00..22.00 rows=1200 width=36)", "warning": None},
            {
                "depth": 3,
                "text": "Seq Scan on authors a  (cost=0.00..22.00 rows=1200 width=36)",
                "warning": "Sequential scan of authors: no index is used",
            },
            {"depth": 4, "text": "Filter: active", "warning": None},
        ],
    }


def test_a_mysql_tree_nests_by_four() -> None:
    plan = plans.explain(
        "SELECT * FROM books",
        None,
        vendor="mysql",
        alias="default",
        execute=lambda *_: ([(MYSQL_TREE,)], ["EXPLAIN"]),
        analyze=True,
    )

    assert plan.to_dict() == {
        "vendor": "mysql",
        "alias": "default",
        "analyzed": True,
        "lines": [
            {
                "depth": 0,
                "text": "Nested loop inner join  (cost=1.6 rows=2) (actual time=0.05..0.07 rows=2 loops=1)",
                "warning": None,
            },
            {
                "depth": 1,
                "text": "Filter: (b.`year` > 2000)  (cost=0.45 rows=1) (actual time=0.03..0.04 rows=2 loops=1)",
                "warning": None,
            },
            {
                "depth": 2,
                "text": "Table scan on b  (cost=0.45 rows=2) (actual time=0.03..0.03 rows=2 loops=1)",
                "warning": None,
            },
            {
                "depth": 1,
                "text": "Index lookup on r using book_id (book_id=b.id)  (cost=0.35 rows=1) "
                "(actual time=0.01..0.01 rows=1 loops=2)",
                "warning": None,
            },
        ],
    }
