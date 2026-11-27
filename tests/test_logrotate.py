from pathlib import Path


def test_nested_poly_dry_log_is_covered_and_bounded():
    config = (Path(__file__).parents[1] / "deploy" / "superbot.logrotate").read_text()
    paths, directives = config.split("{", 1)
    for pattern in (
        "/opt/superbot/local_data/poly_us/*.log",
        "/opt/superbot-releases/current/local_data/*.log",
        "/opt/superbot-releases/current/local_data/poly_us/*.log",
        "/opt/superbot-releases/current/*.log",
    ):
        assert pattern in paths.split()
    for directive in (
        "daily",
        "maxsize 20M",
        "rotate 4",
        "compress",
        "missingok",
        "notifempty",
        "copytruncate",
    ):
        assert directive in {line.strip() for line in directives.splitlines()}
