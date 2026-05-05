from csv_validator import CSVValidator, is_valid_url


VALID_CSV = (
    "service_type,provider,title_en,referral_link\n"
    "credit_card,HDFC,5% cashback,https://hdfc.example.com/ref/a\n"
)


def test_valid_csv_accepted():
    res = CSVValidator(db_dedupe=False).validate_csv_content(VALID_CSV)
    assert res.valid is True
    assert len(res.offers) == 1
    assert res.offers[0].provider == "HDFC"


def test_missing_required_column_rejected():
    csv = "service_type,provider,title_en\ncredit_card,HDFC,5%\n"
    res = CSVValidator(db_dedupe=False).validate_csv_content(csv)
    assert res.valid is False
    assert any("Missing required" in str(e) for e in res.errors)


def test_duplicate_within_csv_deduplicated():
    csv = (
        "service_type,provider,title_en,referral_link\n"
        "credit_card,HDFC,5%,https://hdfc.example.com/ref/a\n"
        "credit_card,HDFC,5%,https://hdfc.example.com/ref/a\n"
    )
    res = CSVValidator(db_dedupe=False).validate_csv_content(csv)
    assert len(res.offers) == 1
    assert any("Duplicate" in w for w in res.warnings)


def test_http_referral_link_rejected():
    csv = (
        "service_type,provider,title_en,referral_link\n"
        "credit_card,HDFC,5%,http://hdfc.example.com/ref/a\n"
    )
    res = CSVValidator(db_dedupe=False).validate_csv_content(csv)
    assert res.valid is False
    assert any("https" in str(e).lower() for e in res.errors)


def test_is_valid_url_https_only_default():
    assert is_valid_url("https://example.com/x") is True
    assert is_valid_url("http://example.com/x") is False
    assert is_valid_url("http://example.com/x", require_https=False) is True
    assert is_valid_url("") is False
    assert is_valid_url("ftp://example.com") is False
