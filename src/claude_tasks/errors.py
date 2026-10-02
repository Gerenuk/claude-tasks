"""The one exception type every refusal and invalid input raises."""


class TasksError(Exception):
    """A refusal or invalid input, naming the offending file, field or value and what was expected."""


class OffDefaultBranch(TasksError):
    """A write refused because the store's checkout is off its default branch. The message names
    no flag of any caller; each caller adds the way out its own interface offers."""
