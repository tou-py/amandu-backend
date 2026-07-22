from django.db import models


class TimestampMixin(models.Model):
    """
    Represents a mixin model for tracking creation and update timestamps.

    This class is designed to be used as an abstract base model for other mixins
    that require fields to automatically record the timestamps of object creation
    and last update. The `created_at` field captures the timestamp when the object
    is created, and the `updated_at` field is updated every time the object is modified.

    :ivar created_at: The date and time when the object was created.
    :type created_at: datetime.datetime
    :ivar updated_at: The date and time when the object was last updated.
    :type updated_at: datetime.datetime
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
