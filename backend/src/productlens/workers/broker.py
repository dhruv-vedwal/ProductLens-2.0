from __future__ import annotations

import dramatiq
from dramatiq.brokers.stub import StubBroker

from productlens.config.settings import Settings


def configure_broker(settings: Settings | None = None) -> dramatiq.Broker:
    """Use RabbitMQ in deployment; retain messages in SQLite regardless of broker choice."""
    settings = settings or Settings.from_environment()
    if settings.broker_url:
        from dramatiq.brokers.rabbitmq import RabbitmqBroker

        broker: dramatiq.Broker = RabbitmqBroker(url=settings.broker_url)
    else:
        # Local development still has durable job records; a worker can be run
        # with a configured broker before production delivery is enabled.
        broker = StubBroker()
    dramatiq.set_broker(broker)
    return broker


broker = configure_broker()
