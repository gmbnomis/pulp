import asyncio
from gettext import gettext as _


class Stage:
    """
    The base class for all Stages API stages.

    To make a stage, inherit from this class and implement :meth:`__call__` on the subclass.
    """

    async def __call__(self, in_q, out_q):
        """
        The coroutine that is run as part of this stage.

        Args:
            in_q (:class:`asyncio.Queue`): The queue to receive items from the previous stage.
            out_q (:class:`asyncio.Queue`): The queue to put handled items into for the next stage.

        Returns:
            The coroutine that runs this stage.

        """
        raise NotImplementedError(_('A plugin writer must implement this method'))


async def create_pipeline(stages, maxsize=100):
    """
    Creates a Stages API linear pipeline from the list `stages` and return as a single coroutine.

    Each stage is a coroutine and reads from an input :class:`asyncio.Queue` and writes to an output
    :class:`asyncio.Queue`. When the stage is ready to shutdown it writes a `None` to the output
    queue. Here is an example of the simplest stage that only passes data.

    >>> async def my_stage(in_q, out_q):
    >>>     while True:
    >>>         item = await in_q.get()
    >>>         if item is None:  # Check if the previous stage is shutdown
    >>>             break
    >>>         await out_q.put(item)
    >>>     await out_q.put(None)  # this stage is shutdown so send 'None'

    Args:
        stages (list of coroutines): A list of Stages API compatible coroutines.
        maxsize (int): The maximum amount of items a queue between two stages should hold. Optional
            and defaults to 100.

    Returns:
        A single coroutine that can be used to run, wait, or cancel the entire pipeline with.
    """
    in_q = None
    futures = []
    for stage in stages:
        out_q = asyncio.Queue(maxsize=maxsize)
        futures.append(stage(in_q, out_q))
        in_q = out_q
    await asyncio.gather(*futures)


class EndStage(Stage):
    """
    A Stages API stage that drains `in_q` and does nothing with the items. This is required at the
    end of all pipelines.

    Without this stage, the `maxsize` of the last stage's `out_q` could fill up and block the entire
    pipeline.
    """

    async def __call__(self, in_q, out_q):
        while True:
            content = await in_q.get()
            if content is None:
                break
