import json
from collections import defaultdict
from datetime import datetime
from typing import Callable, Dict, List, Tuple

from elementary.clients.api.api import APIClient
from elementary.clients.dbt.dbt_runner import DbtRunner
from elementary.config.config import Config
from elementary.monitor.alerts.alert import Alert
from elementary.monitor.alerts.alerts import Alerts, AlertsQueryResult
from elementary.monitor.alerts.malformed import MalformedAlert
from elementary.monitor.alerts.model import ModelAlert
from elementary.monitor.alerts.source_freshness import SourceFreshnessAlert
from elementary.monitor.alerts.test import TestAlert
from elementary.monitor.api.alerts.normalized_alert import NormalizedAlert
from elementary.utils.log import get_logger
from elementary.utils.time import DATETIME_FORMAT, get_now_utc_str

logger = get_logger(__name__)

FALLBACK_TIME = datetime.fromtimestamp(0).strftime(DATETIME_FORMAT)


class AlertsAPI(APIClient):
    def __init__(
        self, dbt_runner: DbtRunner, config: Config, elementary_database_and_schema: str
    ):
        super().__init__(dbt_runner)
        self.config = config
        self.elementary_database_and_schema = elementary_database_and_schema

    def get_new_alerts(self, days_back: int, disable_samples: bool = False) -> Alerts:
        new_test_alerts = self.get_test_alerts(days_back, disable_samples)
        new_model_alerts = self.get_model_alerts(days_back)
        new_source_freshness_alerts = self.get_source_freshness_alerts(days_back)
        return Alerts(
            tests=new_test_alerts,
            models=new_model_alerts,
            source_freshnesses=new_source_freshness_alerts,
        )

    def get_test_alerts(self, days_back: int, disable_samples: bool = False):
        pending_test_alerts = self._query_pending_test_alerts(
            days_back, disable_samples
        )
        last_alert_sent_times = self._query_last_test_alert_times(days_back)
        alerts_to_skip, alerts_to_send = self._sort_alerts(
            pending_test_alerts, last_alert_sent_times
        )
        self._skip_alerts(
            alerts_to_skip=alerts_to_skip, table_name=TestAlert.TABLE_NAME
        )
        return alerts_to_send

    def get_model_alerts(self, days_back: int):
        pending_model_alerts = self._query_pending_model_alerts(days_back)
        last_alert_sent_times = self._query_last_model_alert_times(days_back)
        alerts_to_skip, alerts_to_send = self._sort_alerts(
            pending_model_alerts, last_alert_sent_times
        )
        self._skip_alerts(
            alerts_to_skip=alerts_to_skip, table_name=ModelAlert.TABLE_NAME
        )
        return alerts_to_send

    def get_source_freshness_alerts(self, days_back: int):
        pending_source_freshness_alerts = self._query_pending_source_freshness_alerts(
            days_back
        )
        last_alert_sent_times = self._query_last_source_freshness_alert_times(days_back)
        alerts_to_skip, alerts_to_send = self._sort_alerts(
            pending_source_freshness_alerts, last_alert_sent_times
        )
        self._skip_alerts(
            alerts_to_skip=alerts_to_skip, table_name=SourceFreshnessAlert.TABLE_NAME
        )
        return alerts_to_send

    def _sort_alerts(
        self,
        pending_alerts: AlertsQueryResult[Alert],
        last_alert_sent_times: Dict[str, str],
    ) -> Tuple[List[str], AlertsQueryResult[Alert]]:
        suppressed_alerts = self._get_suppressed_alerts(
            pending_alerts, last_alert_sent_times
        )
        latest_alerts_id = self._get_latest_alerts(pending_alerts)
        alerts_to_skip = []
        alerts_to_send = []
        malformed_alerts_to_send = []

        for alert in pending_alerts.alerts:
            if alert.id in suppressed_alerts or alert.id not in latest_alerts_id:
                alerts_to_skip.append(alert.id)
            else:
                alerts_to_send.append(alert)

        for alert in pending_alerts.malformed_alerts:
            if alert.id in suppressed_alerts or alert.id not in latest_alerts_id:
                alerts_to_skip.append(alert.id)
            else:
                malformed_alerts_to_send.append(alert)

        return alerts_to_skip, AlertsQueryResult(
            alerts=alerts_to_send, malformed_alerts=malformed_alerts_to_send
        )

    def _get_suppressed_alerts(
        self, alerts: AlertsQueryResult[Alert], last_alert_sent_times: Dict[str, str]
    ) -> List[str]:
        suppressed_alerts = []
        current_time_utc = datetime.utcnow()
        for alert in alerts.alerts:
            id = (
                alert.test_unique_id
                if isinstance(alert, TestAlert)
                else alert.unique_id
            )
            suppression_interval = alert.alert_suppression_interval
            last_sent_time = (
                datetime.fromisoformat(last_alert_sent_times.get(id))
                if last_alert_sent_times.get(id)
                else None
            )
            is_alert_in_suppression = (
                (current_time_utc - last_sent_time).seconds / 3600
                <= suppression_interval
                if last_sent_time
                else False
            )
            if is_alert_in_suppression:
                suppressed_alerts.append(alert.id)

        for alert in alerts.malformed_alerts:
            id = alert.data.get("unique_id") or alert.data.get("test_unique_id")
            suppression_interval = alert.data.get("alert_suppression_interval")
            last_sent_time = (
                datetime.fromisoformat(last_alert_sent_times.get(id))
                if last_alert_sent_times.get(id)
                else None
            )
            is_alert_in_suppression = (
                (current_time_utc - last_sent_time).seconds / 3600
                <= suppression_interval
                if last_sent_time
                else False
            )
            if is_alert_in_suppression:
                suppressed_alerts.append(alert.id)

        return suppressed_alerts

    def _get_latest_alerts(self, alerts: AlertsQueryResult[Alert]) -> List[str]:
        alert_last_times = defaultdict(lambda: None)
        latest_alert_ids = []
        for alert in alerts.alerts:
            id = (
                alert.test_unique_id
                if isinstance(alert, TestAlert)
                else alert.unique_id
            )
            current_last_alert = alert_last_times[id]
            alert_detected_at = alert.detected_at.strftime(DATETIME_FORMAT)
            if not current_last_alert:
                alert_last_times[id] = dict(
                    alert_id=alert.id, detected_at=alert_detected_at
                )
            elif current_last_alert["detected_at"] < alert_detected_at:
                alert_last_times[id] = dict(
                    alert_id=alert.id, detected_at=alert_detected_at
                )

        for alert in alerts.malformed_alerts:
            id = alert.data.get("unique_id") or alert.data.get("test_unique_id")
            current_last_alert = alert_last_times[id]
            alert_detected_at = alert.data.get("detected_at").strftime(DATETIME_FORMAT)
            if not current_last_alert:
                alert_last_times[id] = dict(
                    alert_id=alert.id, detected_at=alert_detected_at
                )
            elif current_last_alert["detected_at"] < alert_detected_at:
                alert_last_times[id] = dict(
                    alert_id=alert.id, detected_at=alert_detected_at
                )

        for alert_last_time in alert_last_times.values():
            latest_alert_ids.append(alert_last_time.get("alert_id"))
        return latest_alert_ids

    def _skip_alerts(self, alerts_to_skip: List[str], table_name: str):
        alert_ids_chunks = self._split_list_to_chunks(alerts_to_skip)
        for alert_ids_chunk in alert_ids_chunks:
            self.dbt_runner.run_operation(
                macro_name="update_skipped_alerts",
                macro_args={
                    "alert_ids": alert_ids_chunk,
                    "table_name": table_name,
                },
                json_logs=False,
            )

    def _query_pending_test_alerts(
        self, days_back: int, disable_samples: bool = False
    ) -> AlertsQueryResult[TestAlert]:
        logger.info("Querying test alerts.")
        return self._query_alert_type(
            {
                "macro_name": "get_pending_test_alerts",
                "macro_args": {
                    "days_back": days_back,
                    "disable_samples": disable_samples,
                },
            },
            TestAlert.create_test_alert_from_dict,
        )

    def _query_pending_model_alerts(
        self, days_back: int
    ) -> AlertsQueryResult[ModelAlert]:
        logger.info("Querying model alerts.")
        return self._query_alert_type(
            {
                "macro_name": "get_pending_model_alerts",
                "macro_args": {"days_back": days_back},
            },
            ModelAlert,
        )

    def _query_pending_source_freshness_alerts(
        self, days_back: int
    ) -> AlertsQueryResult[SourceFreshnessAlert]:
        logger.info("Querying source freshness alerts.")
        return self._query_alert_type(
            {
                "macro_name": "get_pending_source_freshness_alerts",
                "macro_args": {"days_back": days_back},
            },
            SourceFreshnessAlert,
        )

    def _query_last_test_alert_times(self, days_back: int) -> Dict[str, str]:
        logger.info("Querying test alerts last sent times.")
        response = self.dbt_runner.run_operation(
            macro_name="get_last_test_alert_sent_times",
            macro_args={"days_back": days_back},
        )
        return json.loads(response[0])

    def _query_last_model_alert_times(self, days_back: int) -> Dict[str, str]:
        logger.info("Querying model alerts last sent times.")
        response = self.dbt_runner.run_operation(
            macro_name="get_last_model_alert_sent_times",
            macro_args={"days_back": days_back},
        )
        return json.loads(response[0])

    def _query_last_source_freshness_alert_times(
        self, days_back: int
    ) -> Dict[str, str]:
        logger.info("Querying source freshness alerts last sent times.")
        response = self.dbt_runner.run_operation(
            macro_name="get_last_source_freshness_alert_sent_times",
            macro_args={"days_back": days_back},
        )
        return json.loads(response[0])

    def _query_alert_type(
        self, run_operation_args: dict, alert_factory_func: Callable
    ) -> AlertsQueryResult:
        raw_alerts = self.dbt_runner.run_operation(**run_operation_args)
        alerts = []
        malformed_alerts = []
        if raw_alerts:
            alert_dicts = json.loads(raw_alerts[0])
            for alert_dict in alert_dicts:
                normalized_alert = self._normalize_alert(alert=alert_dict)
                try:
                    alerts.append(
                        alert_factory_func(
                            elementary_database_and_schema=self.elementary_database_and_schema,
                            timezone=self.config.timezone,
                            **normalized_alert,
                        )
                    )
                except Exception:
                    malformed_alerts.append(
                        MalformedAlert(id=normalized_alert["id"], data=normalized_alert)
                    )
        if malformed_alerts:
            logger.error("Failed to parse some alerts.")
        return AlertsQueryResult(alerts, malformed_alerts)

    @classmethod
    def _normalize_alert(cls, alert: dict) -> dict:
        return NormalizedAlert(alert).get_normalized_alert()

    def update_sent_alerts(self, alert_ids: List[str], table_name: str) -> None:
        alert_ids_chunks = self._split_list_to_chunks(alert_ids)
        for alert_ids_chunk in alert_ids_chunks:
            self.dbt_runner.run_operation(
                macro_name="update_sent_alerts",
                macro_args={
                    "alert_ids": alert_ids_chunk,
                    "sent_at": get_now_utc_str(),
                    "table_name": table_name,
                },
                json_logs=False,
            )

    @staticmethod
    def _split_list_to_chunks(items: list, chunk_size: int = 50) -> List[List]:
        chunk_list = []
        for i in range(0, len(items), chunk_size):
            chunk_list.append(items[i : i + chunk_size])
        return chunk_list
