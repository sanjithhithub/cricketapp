"""Match-level fielding: the wicketkeeper and where every fielder stands.

Deliberately its own module rather than more columns on ``innings``: the
scoring engine never reads any of this, and a bug here must not be able to
touch a ball, a run or a wicket. The only columns added elsewhere are the
foreign keys these tables point at.
"""
