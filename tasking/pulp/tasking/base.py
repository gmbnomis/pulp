import logging
import time
import traceback
import uuid
from contextlib import suppress
from gettext import gettext as _

from celery import Task as CeleryTask, current_task, task
from celery.app import control
from celery.result import AsyncResult
from django.db.models import Count

from pulp.app.models import ReservedResource, Task as TaskStatus, TaskLock, Worker
from pulp.common import TASK_FINAL_STATES, TASK_INCOMPLETE_STATES, TASK_STATES
from pulp.exceptions import MissingResource, PulpException
from pulp.tasking import storage
from pulp.tasking.celery_instance import celery
from pulp.tasking.celery_instance import DEDICATED_QUEUE_EXCHANGE, RESOURCE_MANAGER_QUEUE
from pulp.tasking.constants import TASKING_CONSTANTS

celery_controller = control.Control(app=celery)
_logger = logging.getLogger(__name__)


class PulpTask(CeleryTask):
    """
    The ancestor of all Celery tasks in Pulp. Use the 'base' argument to specify this task as its
    parent. For example:

        >>> from celery import task
        >>> @task(base=PulpTask, acks_late=True)
        >>> def sum(a, b):
        >>>     return a + b

    This object provides a centralized place to put behavioral changes which should affect all
    tasks.
    """
    pass


@task(base=PulpTask, acks_late=True)
def _queue_reserved_task(name, task_id, resource_id, inner_args, inner_kwargs):
    """
    A task that encapsulates another task to be dispatched later. This task being encapsulated is
    called the "inner" task, and a task name, UUID, and accepts a list of positional args
    and keyword args for the inner task. These arguments are named inner_args and inner_kwargs.
    inner_args is a list, and inner_kwargs is a dictionary passed to the inner task as positional
    and keyword arguments using the * and ** operators.

    The inner task is dispatched into a dedicated queue for a worker that is decided at dispatch
    time. The logic deciding which queue receives a task is controlled through the
    find_worker function.

    :param name:          The name of the task to be called
    :type name:           basestring
    :param inner_task_id: The UUID to be set on the task being called. By providing
                          the UUID, the caller can have an asynchronous reference to the inner task
                          that will be dispatched.
    :type inner_task_id:  basestring
    :param resource_id:   The name of the resource you wish to reserve for your task. The system
                          will ensure that no other tasks that want that same reservation will run
                          concurrently with yours.
    :type  resource_id:   basestring

    :return: None
    """
    while True:
        # Find a worker who already has this reservation, it is safe to send this work to them
        try:
            worker = ReservedResource.objects.get(resource=resource_id).worker
        except ReservedResource.DoesNotExist:
            pass
        else:
            break

        try:
            worker = _get_unreserved_worker()
        except Worker.DoesNotExist:
            pass
        else:
            break

        # No worker is ready for this work, so we need to wait
        time.sleep(0.25)

    task_status = TaskStatus.objects.get(pk=task_id)
    ReservedResource.objects.create(task=task_status, worker=worker, resource=resource_id)

    inner_kwargs['routing_key'] = worker.name
    inner_kwargs['exchange'] = DEDICATED_QUEUE_EXCHANGE
    inner_kwargs['task_id'] = task_id

    try:
        celery.tasks[name].apply_async(*inner_args, **inner_kwargs)
    finally:
        _release_resource.apply_async((task_id, ), routing_key=worker.name,
                                      exchange=DEDICATED_QUEUE_EXCHANGE)


def _get_unreserved_worker():
    """
    Find an unreserved Worker

    Return the Worker instance that has no ReservedResource associated with it. If all workers have
    ReservedResource relationships a Worker.DoesNotExist pulp.app.model.Worker exception is
    raised.

    This function also provides randomization for worker selection.

    :raises Worker.DoesNotExist: If all workers have ReservedResource entries associated with them.

    :returns:          The Worker instance that has no reserved_resource
                       entries associated with it.
    :rtype:            pulp.app.model.Worker
    """
    free_workers_qs = Worker.objects.annotate(Count('reservations')).filter(reservations__count=0)
    if free_workers_qs.count() == 0:
        raise Worker.DoesNotExist()
    return free_workers_qs.order_by('?').first()


def delete_worker(name, normal_shutdown=False):
    """
    Delete the Worker with name from the database, cancel any associated tasks and reservations

    If the worker shutdown normally, no message is logged, otherwise an error level message is
    logged. Default is to assume the worker did not shut down normally.

    Any resource reservations associated with this worker are cleaned up by this function.

    Any tasks associated with this worker are explicitly canceled.

    :param name:            The name of the worker you wish to delete.
    :type  name:            basestring
    :param normal_shutdown: True if the worker shutdown normally, False otherwise. Defaults to
                            False.
    :type normal_shutdown:  bool
    """
    if not normal_shutdown:
        msg = _('The worker named %(name)s is missing. Canceling the tasks in its queue.')
        msg = msg % {'name': name}
        _logger.error(msg)

    worker = Worker.objects.get(name=name)

    # Cancel all of the tasks that were assigned to this worker's queue
    for task_status in worker.tasks.filter(state__in=TASK_INCOMPLETE_STATES):
        cancel(task_status.pk)

    worker.delete()

    if name.startswith(TASKING_CONSTANTS.RESOURCE_MANAGER_WORKER_NAME):
        TaskLock.objects.filter(name=name).delete()


@task(base=PulpTask)
def _release_resource(task_id):
    """
    Do not queue this task yourself. It will be used automatically when your task is dispatched by
    the _queue_reserved_task task.

    When a resource-reserving task is complete, this method releases the resource by removing the
    ReservedResource object by UUID.

    :param task_id: The UUID of the task that requested the reservation
    :type  task_id: basestring
    """
    try:
        TaskStatus.objects.get(pk=task_id, state=TASK_STATES.RUNNING)
    except TaskStatus.DoesNotExist:
        pass
    else:
        new_task = PulpTask()
        msg = _('The task status %(task_id)s exited immediately for some reason. Marking as '
                'failed. Check the logs for more details')
        runtime_exception = RuntimeError(msg % {'task_id': task_id})

        class MyEinfo(object):
            traceback = None

        new_task.on_failure(runtime_exception, task_id, (), {}, MyEinfo)

    ReservedResource.objects.get(task__pk=task_id).delete()


class UserFacingTask(PulpTask):
    """
    A Pulp Celery task which will be visible to the user through the tasking portion of the API.

    This object provides two interfaces to dispatch tasks: :meth:`apply_async` and
    :meth:`apply_async_with_reservation`.

    The :meth:`apply_asyc` provides normal celery dispatches of the task to the 'celery' queue
    which all workers subscribe to. The task is handled by the first available worker.

    The :meth:`apply_async_with_reservation` dispatch interface will send the task through the
    resource_manager queue and process and will be assigned a specific worker. A series of
    reservations cause specific types of tasks to not be run concurrently. See the
    :meth:`apply_async_with_reservation` documentation for more details.
    """

    # this tells celery to not automatically log tracebacks for these exceptions
    throws = (PulpException,)

    def apply_async_with_reservation(self, resource_type, resource_id, *args, **kwargs):
        """
        This method provides normal apply_async functionality, while also serializing tasks by
        resource name. No two tasks that claim the same resource name can execute concurrently. It
        accepts resource_type and resource_id and combines them to form a reservation key.

        This does not dispatch the task directly, but instead promises to dispatch it later by
        encapsulating the desired task through a call to a :func:`_queue_reserved_task` task. See
        the docblock on :func:`_queue_reserved_task` for more information on this.

        This method creates a :class:`pulp.app.models.Task` object. Pulp expects to poll on a
        task just after calling this method, so a Task entry needs to exist for it
        before it returns.

        For a list of parameters accepted by the ``*args`` and ``**kwargs`` parameters, please see
        the docblock for the :meth:`apply_async` method.

        :param resource_type: A string that identifies type of a resource
        :type resource_type:  basestring

        :param resource_id:   A string that identifies some named resource, guaranteeing that only
                              one task reserving this same string can happen at a time.
        :type  resource_id:   basestring

        :param tags:          A list of tags (strings) to place onto the task, used for searching
                              for tasks by tag. This is an optional argument which is pulled out of
                              kwargs.
        :type  tags:          list

        :param group_id:      The id to identify which group of tasks a task belongs to. This is an
                              optional argument which is pulled out of kwargs.
        :type  group_id:      uuid.UUID

        :return:              An AsyncResult instance as returned by Celery's apply_async
        :rtype:               celery.result.AsyncResult
        """
        # Form a resource_id for reservation by combining given resource type and id. This way,
        # two different resources having the same id will not block each other.
        resource_id = ":".join((resource_type, resource_id))
        inner_task_id = str(uuid.uuid4())
        task_name = self.name
        tag_list = kwargs.get('tags', [])
        group_id = kwargs.get('group_id', None)

        # Set the parent attribute if being dispatched inside of a Task
        parent_arg = self._get_parent_arg

        # Create a new task status with the task id and tags.
        TaskStatus.objects.create(pk=inner_task_id, state=TaskStatus.WAITING, tags=tag_list,
                                  group=group_id, **parent_arg)

        # Call the outer task which is a promise to call the real task when it can.
        _queue_reserved_task.apply_async(args=[task_name, inner_task_id, resource_id, args, kwargs],
                                         queue=RESOURCE_MANAGER_QUEUE)
        return AsyncResult(inner_task_id)

    def apply_async(self, *args, **kwargs):
        """
        A wrapper around the super() apply_async method. It allows us to accept a few more
        arguments than Celery does for our own purposes, listed below. It also allows us
        to create and update task status which can be used to track status of this task
        during it's lifetime.

        :param queue:       The queue that the task has been placed into (optional, defaults to
                            the general Celery queue named 'celery'.)
        :type  queue:       basestring

        :param tags:        A list of tags (strings) to place onto the task, used for searching for
                            tasks by tag
        :type  tags:        list

        :param group_id:    The id that identifies which group of tasks a task belongs to
        :type group_id:     uuid.UUID

        :return:            An AsyncResult instance as returned by Celery's apply_async
        :rtype:             celery.result.AsyncResult
        """
        tag_list = kwargs.pop('tags', [])
        group_id = kwargs.pop('group_id', None)
        async_result = super(UserFacingTask, self).apply_async(*args, **kwargs)
        async_result.tags = tag_list

        # Set the parent attribute if being dispatched inside of a Task
        parent_arg = self._get_parent_arg

        # Create a new task status with the task id and tags.
        TaskStatus.objects.create(pk=async_result.id, state=TaskStatus.WAITING, tags=tag_list,
                                  group=group_id, **parent_arg)
        return async_result

    def __call__(self, *args, **kwargs):
        """
        Set the :class:`pulp.app.models.Task` object in the running state and log some output.

        Skip the status updating if the task is called synchronously.
        """
        if not self.request.called_directly:
            task_status = TaskStatus.objects.get(pk=self.request.id)
            task_status.set_running()
        _logger.debug("Running task : [%s]" % self.request.id)
        return super(UserFacingTask, self).__call__(*args, **kwargs)

    def on_success(self, retval, task_id, args, kwargs):
        """
        Update the :class:`pulp.app.models.Task` object, log, and save the result.

        Skip the status updating if the callback is called synchronously.

        :param retval:  The return value of the task.
        :type retval:   ???

        :param task_id: Unique id of the executed task.
        :type task_id:  :class:`uuid.UUID`

        :param args:    Original arguments for the executed task.
        :type args:     list

        :param kwargs:  Original keyword arguments for the executed task.
        :type kwargs:   dict
        """
        _logger.debug("Task successful : [%s]" % task_id)
        if not self.request.called_directly:
            task_status = TaskStatus.objects.get(pk=task_id)
            task_status.set_success(retval)

        storage.delete_working_directory()

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """
        Update the :class:`pulp.app.models.Task` object, log, and save the results.

        Skip the status updating if the callback is called synchronously.

        :param exc:     The exception raised by the task.
        :type exc:      ???

        :param task_id: Unique id of the failed task.
        :type task_id:  :class:`uuid.UUID`

        :param args:    Original arguments for the executed task.
        :type args:     list

        :param kwargs:  Original keyword arguments for the executed task.
        :type kwargs:   dict

        :param einfo:   celery's ExceptionInfo instance, containing serialized traceback.
        :type einfo:    ???
        """
        _logger.error(_('Task failed : [%s]') % task_id)

        if not self.request.called_directly:
            task_status = TaskStatus.objects.get(pk=task_id)
            task_status.set_failed(exc, einfo)

        storage.delete_working_directory()

    def _get_parent_arg(self):
        """Return a dictionary with the parent set if running inside of a Task"""
        parent_arg = {}
        current_task_id = get_current_task_id()
        if current_task_id is not None:
            current_task_obj = TaskStatus.objects.get(current_task_id)
            parent_arg['parent'] = current_task_obj
        return parent_arg


def cancel(task_id):
    """
    Cancel the task that is represented by the given task_id. This method cancels only the task
    with given task_id, not the spawned tasks. This also updates task's state to 'canceled'.

    :param task_id: The ID of the task you wish to cancel
    :type  task_id: basestring

    :raises MissingResource: if a task with given task_id does not exist
    """
    try:
        task_status = TaskStatus.objects.get(pk=task_id)
    except TaskStatus.DoesNotExist:
        raise MissingResource(task_id)

    if task_status.state in TASK_FINAL_STATES:
        # If the task is already done, just stop
        msg = _('Task [%(task_id)s] already in a completed state: %(state)s')
        _logger.info(msg % {'task_id': task_id, 'state': task_status.state})
        return

    celery_controller.revoke(task_id, terminate=True)
    task_status.state = TaskStatus.CANCELED
    task_status.save()

    msg = _('Task canceled: %(task_id)s.')
    msg = msg % {'task_id': task_id}
    _logger.info(msg)


def get_current_task_id():
    """"
    Get the current task id from celery. If this is called outside of a running
    celery task it will return None

    :return: The ID of the currently running celery task or None if not in a task
    :rtype: str
    """
    with suppress(AttributeError):
        return current_task.request.id
