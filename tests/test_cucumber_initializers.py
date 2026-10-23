"""Initializers serveur du client : détection, créations extraites, codes résolus, indice sur une donnée absente."""
import os

from tests.conftest import git, write
from tools import cucumber_initializers as ci

MAIN = "core/src/main/java/app/init"
INITIALIZER = """package app.init;

import static app.constant.DocConstant.DOC_CODE;

@Component
public class DocDatabaseInitializer implements AutoPreparable, DatabaseInitializer {

    private final TransactionService transactionService;
    private final RefReportDocRepository refReportDocRepository;
    private final ParamLineRepository paramLineRepository;
    private final ProviderMessageService providerMessageService;
    private final TimeService timeService;

    @Override
    public void initialize() {
        transactionService.execute(this::initializeDoc);
        initializeLines(param);
        providerMessageService.synchronize(param);
    }

    private void initializeDoc() {
        refReportDocRepository.initByNaturalId(DOC_CODE, refReportDocRepository.create(DOC_CODE));
        refReportDocRepository.initByNaturalId(consumer.getCode(), consumer);
    }

    private void initializeLines(Param param) {
        paramLineRepository.existOrCreateAndSaveLine(param, ParamConstant.LINE_LOGIN, "User");
        List<Reason> reasons = new ArrayList<>();
        reasons.add(new BlockingReason("LITERAL_REASON", "label"));
    }

    public Instant notCalledByInitialize() {
        refReportDocRepository.initByNaturalId("NEVER", null);
        return timeService.instantNow();
    }
}
"""


def _repo(path):
    os.makedirs(path)
    git(path, "init", "-q", "-b", "integration")
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")
    return path


def test_extract_creations_follows_initialize():
    creations = ci.extract_creations(INITIALIZER)
    found = {(c["entite"], c["expression"], c["mode"], c["certitude"]) for c in creations}
    assert found == {("RefReportDoc", "DOC_CODE", "initByNaturalId", "certain"),
                     ("RefReportDoc", "consumer.getCode()", "initByNaturalId", "partiel"),
                     ("ParamLine", "ParamConstant.LINE_LOGIN", "existOrCreateAndSaveLine", "certain"),
                     ("BlockingReason", '"LITERAL_REASON"', "new", "certain"),
                     ("", "", "appel de service ProviderMessageService.synchronize", "partiel")}
    assert ci.static_imports(INITIALIZER) == {"DOC_CODE": "DocConstant"}


def test_find_initializers_in_repos_with_specifics(tmp_path, monkeypatch):
    monkeypatch.delenv("CUCUMBER_INITIALIZER_PATTERN", raising=False)
    monkeypatch.delenv("CUCUMBER_INITIALIZER_EXCLUDE_REPOS", raising=False)
    client_root, std_root = tmp_path / "client", tmp_path / "std"
    app, other = _repo(str(client_root / "app")), _repo(str(client_root / "other"))
    std_app = _repo(str(std_root / "app"))
    standard = "package app.init;\npublic class StdInitializer implements DatabaseInitializer {}\n"
    for repo in (app, std_app):
        write(os.path.join(repo, MAIN, "StdInitializer.java"), standard)
    write(os.path.join(app, MAIN, "DocDatabaseInitializer.java"), INITIALIZER)
    write(os.path.join(app, "core/src/main/java/app/constant/DocConstant.java"),
          'public class DocConstant {\n    public static final String DOC_CODE = PREFIX + "INVOICE";\n'
          '    public static final String PREFIX = "SPEC_";\n}\n')
    write(os.path.join(app, "core/src/test/java/app/TestInitializer.java"),
          "public class TestInitializer implements DatabaseInitializer {}\n")
    write(os.path.join(std_app, "core/src/main/java/app/constant/ParamConstant.java"),
          'public class ParamConstant { public static final String LINE_LOGIN = "LOGIN"; }\n')
    write(os.path.join(other, MAIN, "OtherInitializer.java"), INITIALIZER)
    for repo in (app, other, std_app):
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")
    inventory = {"depot_client": str(client_root), "standard": str(std_root),
                 "specifiques": [{"depot": "app"}], "references": {"app": {"ref": "integration"}}}
    found = ci.find_initializers(inventory)
    # Dépôt sans spécifique, initializer identique au Standard et code de test écartés
    assert [(i["depot"], i["classe"]) for i in found] == [("app", "DocDatabaseInitializer")]
    codes = {c["expression"]: (c["code"], c["certitude"]) for c in found[0]["creations"]}
    assert codes["DOC_CODE"] == ("SPEC_INVOICE", "certain")
    assert codes["ParamConstant.LINE_LOGIN"] == ("LOGIN", "certain")
    assert codes['"LITERAL_REASON"'] == ("LITERAL_REASON", "certain")
    assert codes["consumer.getCode()"] == ("", "partiel")
    monkeypatch.setenv("CUCUMBER_INITIALIZER_EXCLUDE_REPOS", "ap*")
    assert ci.find_initializers(inventory) == []
    monkeypatch.delenv("CUCUMBER_INITIALIZER_EXCLUDE_REPOS")
    monkeypatch.setenv("CUCUMBER_INITIALIZER_PATTERN", r"implements\s+NoSuchInterface")
    assert ci.find_initializers(inventory) == []


def test_reference_hint_on_initializer_code():
    data = ci.initialized_data([{"depot": "app", "chemin": "A.java", "classe": "DocDatabaseInitializer",
                                 "creations": [{"entite": "RefReportDoc", "code": "SPEC_INVOICE",
                                                "expression": "DOC_CODE", "mode": "initByNaturalId",
                                                "certitude": "certain"}]}])
    hint = ci.reference_hint("404 - Document non trouvé(e) avec code SPEC_INVOICE", data)
    assert "DocDatabaseInitializer" in hint and "ne survit sans doute pas au clear" in hint
    assert ci.reference_hint("404 - Document non trouvé(e) avec code AUTRE", data) == ""
    assert ci.usable({"statut": "inconnu"}, "vrai") and not ci.usable({"statut": "inconnu"}, "")
    assert ci.usable({"statut": "recreee"}, "faux")
