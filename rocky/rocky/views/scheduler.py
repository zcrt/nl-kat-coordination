import uuid
from datetime import datetime
from typing import Any

from django.contrib import messages
from django.http import JsonResponse
from django.utils.translation import gettext_lazy as _
from katalogus.client import Boefje, Normalizer
from tools.forms.scheduler import TaskFilterForm

from octopoes.models import OOI
from rocky.scheduler import Boefje as SchedulerBoefje
from rocky.scheduler import BoefjeTask, LazyTaskList, NormalizerTask, RawData, SchedulerError, Task, scheduler_client
from rocky.scheduler import Normalizer as SchedulerNormalizer
from rocky.views.mixins import OctopoesView


def get_date_time(date: str | None) -> datetime | None:
    if date:
        return datetime.strptime(date, "%Y-%m-%d")
    return None


class SchedulerView(OctopoesView):
    task_type: str
    task_filter_form = TaskFilterForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.scheduler_client = scheduler_client(self.organization.code)

    def get_task_filters(self) -> dict[str, Any]:
        return {
            "scheduler_id": f"{self.task_type}-{self.organization.code}",
            "task_type": self.task_type,
            "plugin_id": None,  # plugin_id present and set at plugin detail
            **self.get_task_filter_form_data(),
        }

    def get_task_filter_form_data(self) -> dict[str, Any]:
        form_data = self.get_task_filter_form().data.dict()
        return {k: v for k, v in form_data.items() if v}

    def count_active_task_filters(self):
        form_data = self.get_task_filter_form_data()

        count = len(form_data)
        for task_filter in form_data:
            if task_filter in ("observed_at", "ooi_id"):
                count -= 1

        return count

    def get_task_filter_form(self) -> TaskFilterForm:
        return self.task_filter_form(self.request.GET)

    def get_task_list(self) -> LazyTaskList | list[Any]:
        try:
            return LazyTaskList(self.scheduler_client, **self.get_task_filters())
        except SchedulerError as error:
            messages.error(self.request, error.message)
        return []

    def get_task_details(self, task_id: str) -> Task | None:
        try:
            return self.scheduler_client.get_task_details(task_id)
        except SchedulerError as error:
            return messages.error(self.request, error.message)

    def get_task_statistics(self) -> dict[Any, Any]:
        stats = {}
        try:
            stats = self.scheduler_client.get_task_stats(self.task_type)
        except SchedulerError as error:
            messages.error(self.request, error.message)
        return stats

    def get_output_oois(self, task):
        try:
            return self.octopoes_api_connector.list_origins(
                valid_time=task.data.raw_data.boefje_meta.ended_at,
                task_id=task.id,
            )[0].result
        except IndexError:
            return []
        except SchedulerError as error:
            messages.error(self.request, error.message)
            return []

    def get_json_task_details(self) -> JsonResponse | None:
        try:
            task = self.get_task_details(self.kwargs["task_id"])
            if task:
                return JsonResponse(
                    {
                        "oois": self.get_output_oois(task),
                        "valid_time": task.data.raw_data.boefje_meta.ended_at.strftime("%Y-%m-%dT%H:%M:%S"),
                    },
                    safe=False,
                )
            return task
        except SchedulerError as error:
            return messages.error(self.request, error.message)

    def schedule_task(self, task: Task) -> None:
        if not self.indemnification_present:
            return self.indemnification_error()
        try:
            self.scheduler_client.push_task(task)
        except SchedulerError as error:
            messages.error(self.request, error.message)
        else:
            messages.success(
                self.request,
                _(
                    "Your task is scheduled and will soon be started in the background. "
                    "Results will be added to the object list when they are in. "
                    "It may take some time, a refresh of the page may be needed to show the results."
                ),
            )

    # FIXME: Tasks should be (re)created with supplied data, not by fetching prior
    # task info from the scheduler. Task data should be available from the context
    # from which the task is created.
    def reschedule_task(self, task_id: str) -> None:
        try:
            task = self.scheduler_client.get_task_details(task_id)

            new_id = uuid.uuid4()
            task.data.id = new_id

            new_task = Task(
                id=new_id,
                scheduler_id=task.scheduler_id,
                priority=1,
                data=task.data,
            )

            self.schedule_task(new_task)
        except SchedulerError as error:
            messages.error(self.request, error.message)

    def run_normalizer(self, katalogus_normalizer: Normalizer, raw_data: RawData) -> None:
        try:
            normalizer_task = NormalizerTask(
                normalizer=SchedulerNormalizer.model_validate(katalogus_normalizer.model_dump()),
                raw_data=raw_data,
            )

            new_task = Task(
                priority=1,
                data=normalizer_task,
                scheduler_id=f"normalizer-{self.organization.code}",
            )

            self.schedule_task(new_task)
        except SchedulerError as error:
            messages.error(self.request, error.message)

    def run_boefje(self, katalogus_boefje: Boefje, ooi: OOI | None) -> None:
        try:
            boefje_task = BoefjeTask(
                boefje=SchedulerBoefje.model_validate(katalogus_boefje.model_dump()),
                input_ooi=ooi.reference if ooi else None,
                organization=self.organization.code,
            )

            new_task = Task(
                priority=1,
                data=boefje_task,
                scheduler_id=f"boefje-{self.organization.code}",
            )

            self.schedule_task(new_task)

        except SchedulerError as error:
            messages.error(self.request, error.message)

    def run_boefje_for_oois(
        self,
        boefje: Boefje,
        oois: list[OOI],
    ) -> None:
        try:
            if not oois and not boefje.consumes:
                self.run_boefje(boefje, None)

            for ooi in oois:
                if ooi.scan_profile and ooi.scan_profile.level < boefje.scan_level:
                    self.can_raise_clearance_level(ooi, boefje.scan_level)
                self.run_boefje(boefje, ooi)
        except SchedulerError as error:
            messages.error(self.request, error.message)
