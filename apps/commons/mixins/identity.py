import uuid

from django.db import models


class PublicIdentifierMixin(models.Model):
    """
    Primary key for models whose id travels to a client the project does not control.

    Use it when the id appears in a URL or an API payload. Sequential integers leak
    two things there: they invite probing one row at a time, and the difference
    between two ids tells a competitor how many rows were created in between.
    Do NOT use it on internal catalogue or join tables -- a UUID costs 16 bytes in
    every foreign key and index that points at it, and is miserable to type while
    debugging.

    Generation is uuid7, not uuid4, and the difference is not cosmetic. uuid4 lands
    on a random page of the primary key index, so once the table outgrows memory,
    every insert is a disk read plus a page split. uuid7 puts a millisecond timestamp
    in its leading bits, so inserts stay ordered and append at the end, like a
    sequence. The price is that a uuid7 reveals WHEN the row was created -- if that
    is a secret, this mixin is the wrong tool.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)

    class Meta:
        abstract = True
