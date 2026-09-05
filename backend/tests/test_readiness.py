from productlens.observability.readiness import probe_broker, probe_database, probe_object_storage


class Broker:
    def declare_queue(self, name):
        assert name == "productlens-readiness"


class Storage:
    bucket = "productlens"

    class Client:
        def head_bucket(self, **kwargs):
            assert kwargs == {"Bucket": "productlens"}

    client = Client()


def test_broker_readiness_probe_is_provider_neutral():
    assert probe_broker(Broker())["ready"] is True


def test_object_storage_readiness_probe_checks_bucket_access():
    report = probe_object_storage(Storage())
    assert report == {"ready": True, "provider": "Storage", "bucket": "productlens"}


def test_provider_probe_reports_failures_without_raising():
    class Broken:
        bucket = "missing"
        client = object()

    assert probe_object_storage(Broken())["ready"] is False


def test_database_probe_runs_sentinel_query():
    class Cursor:
        def execute(self, query):
            assert query == "SELECT 1"

        def fetchone(self):
            return (1,)

    class Connection:
        def cursor(self):
            return Cursor()

    assert probe_database(Connection())["ready"] is True
