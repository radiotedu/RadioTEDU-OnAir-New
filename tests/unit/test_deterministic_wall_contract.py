import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ONAIR = ROOT / "app" / "static" / "onair"


class _OperatorControlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.form_stack: list[str] = []
        self.forms: set[str] = set()
        self.buttons: list[tuple[str, str, str | None, set[str]]] = []

    def handle_starttag(self, tag: str, attrs):
        attributes = dict(attrs)
        if tag == "form":
            form_id = attributes.get("id")
            if form_id:
                self.forms.add(form_id)
                self.form_stack.append(form_id)
        if tag == "button" and attributes.get("id"):
            data_attributes = {name for name, _ in attrs if name.startswith("data-")}
            self.buttons.append(
                (
                    attributes["id"],
                    attributes.get("type", "submit").lower(),
                    self.form_stack[-1] if self.form_stack else None,
                    data_attributes,
                )
            )

    def handle_endtag(self, tag: str):
        if tag == "form" and self.form_stack:
            self.form_stack.pop()


def test_onair_exposes_every_self_service_control():
    html = (ONAIR / "index.html").read_text(encoding="utf-8")
    required_ids = {
        "startBroadcastButton",
        "stopBroadcastButton",
        "broadcastAutostartEnabled",
        "stationForm",
        "currentOutputForm",
        "currentIcecastTlsEnabled",
        "testCurrentOutputButton",
        "libraryFolderForm",
        "librarySkipUnplayable",
        "browseLibraryFolderButton",
        "librarySearchForm",
        "queueList",
        "jingleUploadForm",
        "jingleFolderForm",
        "browseJingleFolderButton",
        "sweeperForm",
        "sweeperInterval",
        "sweeperMode",
        "aiConfigForm",
        "testAiButton",
        "refreshReadinessButton",
        "repairDependenciesButton",
        "passwordForm",
        "startEmergencyButton",
        "emergencyPreset",
        "previewEmergencyButton",
        "operatorNavigation",
        "timelineRemaining",
        "forecastList",
    }
    ids = set(re.findall(r'\bid="([A-Za-z][A-Za-z0-9_-]*)"', html))
    assert required_ids.issubset(ids)
    assert len(ids) == len(re.findall(r'\bid="([A-Za-z][A-Za-z0-9_-]*)"', html))


def test_operator_mutations_use_read_back_verification_and_safe_retry():
    javascript = (ONAIR / "app.js").read_text(encoding="utf-8")
    stylesheet = (ONAIR / "styles.css").read_text(encoding="utf-8")
    assert "async function verifiedMutation" in javascript
    assert "idempotent: true" in javascript
    assert "async function saveCurrentOutput" in javascript
    assert "icecast_tls_enabled: $('currentIcecastTlsEnabled').checked" in javascript
    assert "state.setupState?.blocking_reasons" in javascript
    assert "check.required === false" in javascript
    assert "readiness-list li.optional::before" in stylesheet
    assert ".file-drop { position: relative; overflow: hidden;" in stylesheet
    assert ".file-drop input { position: absolute; inset: 0;" in stylesheet
    assert "node.type !== 'checkbox'" in javascript
    assert "async function saveAiConfiguration" in javascript
    assert "async function syncJingleFolder" in javascript
    assert "async function changePassword" in javascript
    assert "async function startEmergency" in javascript
    assert "async function addTrackToQueue" in javascript
    assert "async function startBroadcast" in javascript
    assert "let refreshSessionPromise = null" in javascript
    assert "if (refreshSessionPromise) return refreshSessionPromise" in javascript
    assert "function isBroadcastVerifiedLive" in javascript
    assert "isBroadcastVerifiedLive(publicStation)" in javascript
    assert "runtime_delivery_health || runtime.delivery_health" in javascript
    assert "publicStation?.preserved_item" in javascript
    assert "Verified end to end" in javascript
    assert "async function updateBroadcastAutostartFromControl" in javascript
    assert "addEventListener('change', updateBroadcastAutostartFromControl)" in javascript
    assert "async function stopBroadcast" in javascript
    assert "/operator-stop" in javascript
    assert "/operator-start-track" in javascript
    assert "/operator-supervise" in javascript
    assert "interval_unit: 'tracks'" in javascript
    assert "'sweeperEnabled', 'sweeperInterval', 'sweeperMode'" in javascript
    assert "setCleanChecked('sweeperEnabled'" in javascript
    assert "setCleanValue('sweeperMode'" in javascript
    assert "settings.library_active_files" in javascript
    assert "skip_unplayable: $('librarySkipUnplayable').checked" in javascript
    assert "user = await api('/api/auth/me')" in javascript
    assert "!live || state.emergency.starting || state.emergency.stopping" in javascript
    assert "serviceControlState" in javascript
    html = (ONAIR / "index.html").read_text(encoding="utf-8")
    assert "https://radyo.trt.net.tr/kanallar/radyo-1" in html
    assert "https://stream.radiotedu.com/lofi" not in html
    assert "activateOperatorView" in javascript
    assert "pull_model" in javascript
    assert "update_repository" in javascript
    assert "/api/operator/pick-file" in javascript
    assert "radiotedu-picker-request" in javascript
    assert "async function pickOperatorPath" in javascript
    assert "data-service-path=" in javascript
    assert "database.last_update_at" in javascript
    assert "if (!state.stationId || (state.busy && !silent)) return;" in javascript
    assert "Stop stream — keep playlist" in html
    assert "AI is content-only" in html


def test_every_static_operator_control_has_a_dom_target_and_event_owner():
    html = (ONAIR / "index.html").read_text(encoding="utf-8")
    javascript = (ONAIR / "app.js").read_text(encoding="utf-8")
    guest_javascript = (ONAIR / "guest-room.js").read_text(encoding="utf-8")
    all_javascript = f"{javascript}\n{guest_javascript}"

    ids = re.findall(r'\bid="([A-Za-z][A-Za-z0-9_-]*)"', html)
    assert len(ids) == len(set(ids)), "Duplicate HTML ids make button routing nondeterministic"
    id_set = set(ids)

    referenced_ids = set(re.findall(r"\$\('([^']+)'\)", javascript))
    referenced_ids.update(re.findall(r"byId\('([^']+)'\)", guest_javascript))
    assert referenced_ids <= id_set, f"JavaScript references missing DOM ids: {sorted(referenced_ids - id_set)}"

    parser = _OperatorControlParser()
    parser.feed(html)
    direct_bindings = set(
        re.findall(r"(?:\$|byId)\('([^']+)'\)\.addEventListener\(", all_javascript)
    )
    submit_bindings = set(
        re.findall(
            r"(?:\$|byId)\('([^']+)'\)\.addEventListener\('submit'",
            all_javascript,
        )
    )

    unowned: list[str] = []
    for button_id, button_type, form_id, data_attributes in parser.buttons:
        if button_id in direct_bindings:
            continue
        if data_attributes and "document.addEventListener('click'" in javascript:
            continue
        if button_type == "submit" and form_id in submit_bindings:
            continue
        unowned.append(button_id)

    assert not unowned, f"Static buttons without an event owner: {sorted(unowned)}"
    assert parser.forms <= submit_bindings, (
        "Forms without deterministic submit handlers: "
        f"{sorted(parser.forms - submit_bindings)}"
    )
