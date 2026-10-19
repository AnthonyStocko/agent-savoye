# Conventions de développement spécifique

Document de référence pour les sessions de travail sur le spécifique Odatio.
À lire avant toute création de composant. Les exemples cités existent réellement dans le code
et servent de modèle à copier.

---

## 1. Dépôts et arborescence

| Chemin | Contenu |
|---|---|
| répertoire courant (`feat_xxx_SPEC`) | la branche feature du client : tout le code spécifique sur lequel on travaille |
| `C:\Users\cfontimpe\.m2\repository` | repository Maven contenant l'ensemble du code du socle standard que tu n'as pas dans le projet. **C'est la seule référence qui fait foi pour le standard** |

Sous `feat_xxx_SPEC`, xxx correspond au client concerné :

| Dépôt | Rôle |
|---|---|
| `odatio-xxx` | tout le back spécifique (datamodel, datarepository, core, rule, action, rest) |
| `odatio-ui-xxx` | les écrans spécifiques (plugins UI AngularJS) |
| `studio/` | processus interactifs spécifiques et forkés et IHM Studio (`customized.xml` + `customized.json`) |
| `xxx-odatio-app` | l'assemblage : `bundle.list` |
| `wms-goodsin-backend`, `wms-preparation-backend`, `...` | forks du standard a-sis |

`odatio-xxx` est découpé par domaine fonctionnel : `xxx-ref-organization`, `xxx-ref-product`,
`xxx-wms-content`, `xxx-wms-goodsin`, `xxx-wms-cartography`, `xxx-util`.
Chaque domaine contient les modules `-datamodel`, `-datarepository`, `-core`, `-rule`, `-action`, `-rest`.

**Règle de placement** : un composant va dans le domaine de l'entité standard qu'il manipule
principalement, pas dans celui du processus qui l'appelle.

---

## 2. Nommage

Tout ce qui est spécifique est préfixé `Spec` / `spec-` / `spec_`, sans exception.

| Élément | Convention | Exemple |
|---|---|---|
| Classe Java | `Spec` + nom métier | `SpecWmsRepackingBoxAllocation` |
| Package | `xxx.spec.<domaine>.<couche>` | `xxx.spec.wms.goodsin.datamodel` |
| Composant KIS | `xxx/spec-<domaine>-<couche>[-<nom>]` | `xxx/spec-wms-goodsin-action-load-boxproduct` |
| Table SQL | `spec_<domaine>_<nom>` | `spec_wms_repacking_box_allocation` |
| Méthode de repository spécifique | préfixe `spec` | `specFindAllocation(...)` |
| Composant UI | `xxx/spec-<domaine>-ui-<nom>` | `xxx/spec-wms-goodsin-ui-temporaryhu` |

Le nom du répertoire d'une action reflète son nom de composant :
`action/load/boxproduct/` donne `xxx/spec-wms-goodsin-action-load-boxproduct`.

---

## 3. Créer une entité spécifique

Modèle à copier : `SpecWmsGoodsinTemporaryHU` / `SpecWmsGoodsinTemporaryHUDetail`
(`odatio-xxx/xxx-wms-goodsin/xxx-spec-wms-goodsin-datamodel`).

### 3.1 La classe

```java
@Entity
@Table(indexes = {
        @Index(name = "ux_spec_<nomCamel>", columnList = SpecXxx.COLUMN_INDEX, unique = true),
        @Index(name = "ix_<abrev>_<colonne>", columnList = SpecXxx.COLUMN_Y)
})
@AttributeOverrides({
        @AttributeOverride(name = "id", column = @Column(length = 16, name = SpecXxx.COLUMN_ID))})
@Universe(GoodsInUniverse.class)
public class SpecXxx extends AuditableVersionableUUIDEntity {

    public static final String COLUMN_ID = "xxxId";
    public static final String COLUMN_INDEX = COLUMN_A + "," + COLUMN_B;
    public static final int LNG_A = 20;

    @NaturalId(mutable = false)
    @Column(name = COLUMN_A, length = LNG_A, nullable = false)
    private String a;

    SpecXxx() { }                       // JPA constructor, package-private

    public SpecXxx(String a) {
        this();
        generateId();                   // MANDATORY
        this.a = a;
    }
}
```

### 3.2 Règles impératives

- **Jamais d'UUID d'une entité standard dans une table spécifique.** On stocke le *natural id* :
  code UM, code produit, code donneur d'ordre, `num` de l'ordre logistique (`WmsLogisticOrder.num`,
  annoté `@NaturalId`), etc. Une FK JPA est acceptable entre deux entités spécifiques
  (cf. `SpecWmsGoodsinTemporaryHUDetail` vers `SpecWmsGoodsinTemporaryHU`), mais vers le standard
  on utilise le code.
- Toutes les constantes de colonnes et de longueurs en `public static final`, jamais de littéral
  dans les annotations.
- Les longueurs s'alignent sur celles du standard : `WmsHandlingUnit.LNG_CODE = 20`,
  `RefStockOwner.LNG_CODE = 20`, `RefProduct.LNG_CODE = 40`. On redéclare la valeur en local
  (le module datamodel ne dépend pas forcément de `ref-product-datamodel`) avec un commentaire
  indiquant l'alignement.
- `@Universe(...)` est obligatoire : il conditionne le filtrage REST et la visibilité écran.
- Le nom d'index unique est repris tel quel dans le script SQL (`ux_spec_...`).

### 3.3 Les scripts SQL

Un fichier `V<n>-xxx-spec-<domaine>-datamodel.sql` **par SGBD**, dans
`src/main/resources/xxx/spec/<domaine>/datamodel/scripts/` :

```
derby/  oracle/  postgres/  sql_server/
```

`<n>` = version suivante non utilisée (regarder les fichiers existants). Les 4 fichiers doivent
exister, même si le contenu est quasi identique.

Colonnes systématiques héritées de `AuditableVersionableUUIDEntity`, à ne pas oublier :
`postponed_cleanup`, `created_by`, `created_date`, `last_modified_by`, `last_modified_date`, `version`.

Correspondance des types :

| Java | postgres | oracle | sql_server | derby |
|---|---|---|---|---|
| UUID (id) | `uuid` | `raw(16)` | `uniqueidentifier` | `char(16) for bit data` |
| boolean | `boolean` | `number(1,0)` | `bit` | `boolean` |
| String(n) | `varchar(n)` | `varchar2(n char)` | `varchar(n)` | `varchar(n)` |
| long | `int8` | `number(19,0)` | `bigint` | `bigint` |
| int (version) | `int4` | `number(10,0)` | `int` | `integer` |
| Instant | `timestamp` | `timestamp` | `datetime2` | `timestamp` |

L'index unique se déclare en `alter table ... add constraint` sur postgres / oracle / sql_server,
et en `create unique index` sur derby.

Commencer le script par un commentaire `-- SPEC-<ticket> : <objet>`.

**Le script doit être joué sur la base avant de démarrer l'appli**, sinon la première écriture échoue.

### 3.4 Les traductions de l'entité

Dans `src/main/resources/xxx/spec/<domaine>/datamodel/i18n.fr_FR.txt` et `i18n.en_GB.txt`,
un bloc par entité, séparé par une ligne vide :

```
xxx.spec.wms.goodsin.datamodel.SpecXxx:Libellé entité
SpecXxx:Libellé entité
specXxx.champA:Libellé champ
specXxx.version:Version
specXxx.createdBy:Créé par
specXxx.createdDate:Créé le
specXxx.lastModifiedBy:Modifié par
specXxx.lastModifiedDate:Modifié le
```

Les 4 champs d'audit et `version` sont à déclarer à chaque fois : ils sont affichés dans les grilles.

Un champ **ajouté plus tard** à une entité existante suit la même règle : sans sa clé dans les deux
fichiers, la grille affiche le nom technique du champ (`refBoxProductCode` au lieu de
« Carton réapprovisionné »). Rien ne le signale au build (§17bis).

---

## 4. Repository

Module `-datarepository`, classe `SpecXxxRepository` :

```java
@DataRepository
public class SpecXxxRepository extends DefaultDataRepository<SpecXxx, UUID> {

    public SpecXxxRepository(SessionFactory sessionFactory) {
        super(sessionFactory);
    }

    public Optional<SpecXxx> specFindByY(String y) {
        return Optional.ofNullable(getQueryFactory()
                .selectFrom(specXxx)
                .where(specXxx.y.eq(y))
                .fetchFirst());
    }
}
```

- Import statique de la Q-classe générée : `import static xxx...QSpecXxx.specXxx;`
  (générée par `kis-persistence-core-annotation-processor`, disponible après un premier build).
- `DefaultDataRepository` fournit `find()`, `find(Consumer<QueryParams>)`, `count()`, `save()`,
  `saveAndFlush()`, `delete()`, `deleteQuery()`.
- `DefaultListenerDataRepository` seulement s'il faut des listeners de suppression.
- Un repository spécifique peut aussi porter des accesseurs sur les **champs custom d'une entité
  standard**, pour donner un nom métier à un `customStringN` — voir `SpecWmsLogisticOrderRepository`
  (`isBoxesDegradedModeReplenishmentOrder` sur `customString1`,
  `getOriginHandlingUnitCodeOfBoxReplenishmentOrder` sur `customString2`).
  **Toujours passer par ces accesseurs, jamais par `getCustomStringN()` directement dans le code
  métier** : c'est la seule documentation de ce que contient le champ.

---

## 5. Service (module `-core`)

```java
@Component
public class SpecXxxServiceImpl implements SpecXxxService {
    private static final Logger logger = getLogger(SpecXxxServiceImpl.class);
    // constructor injection only, no annotation on fields
}
```

Toujours une interface plus un impl. L'interface est ce que consomment les actions.

---

## 6. Action Studio (module `-action`)

```java
/**
 * Action xxx/spec-<domaine>-action-<nom>
 */
@Action
public class SpecXxxAction implements Serializable {

    private static final Logger logger = getLogger(SpecXxxAction.class);

    @In  WmsHandlingUnit wmsHandlingUnit;               // inputs
    @Out List<UUID> results;                            // outputs

    @Inject transient SpecXxxService specXxxService;    // always transient

    @Execute
    void execute() { }
}
```

- Les champs injectés sont **`transient`** (l'action est sérialisée dans la session Studio).
- Le descripteur XML de l'action est **généré** dans `target/classes` par
  `kis-engine-core-annotation-processor` — ne jamais l'écrire ni le modifier à la main.
- Les noms des `@In` / `@Out` sont le contrat avec le processus Studio : les renommer casse le
  `customized.xml` qui les référence. **En ajouter est sans risque** : un `@Out` que le
  `customized.xml` ne mappe pas est simplement ignoré. C'est la façon d'enrichir une action
  existante sans toucher au processus.

---

## 6bis. Règle et profil (module `-rule`)

Une règle rend un comportement **paramétrable par le client** depuis l'écran des règles. Deux
classes, dans deux packages :

```
...rule.<nom>/SpecXxxRuleDefinition.java        les branches (l'arbre de décision)
...profile.<nom>/SpecXxxProfileDefinition.java  les paramètres et les configurations
```

Modèle : `SpecBypassRuleDefinition` / `SpecBypassProfileDefinition` (règle `SPEC-007`).

```java
@Universe(GoodsInUniverse.class)
@Rule(profile = SpecXxxProfileDefinition.class, description = "...")
public class SpecXxxRuleDefinition {

    @In(description = "...")
    public WmsReceptionFolder wmsReceptionFolder;      // context exposed to the conditions

    @InitRule(value = "SpecXxx", description = "...", code = "SPEC-0NN")
    public Expressions ruleInit = Expressions.of()
            .when("wmsReceptionFolder.receiptType.id == 'RETURNS_C'").desc("Returns C")
            .then(SpecXxxProfile.CONFIG_A)
            .otherwise(SpecXxxProfile.CONFIG_DEFAUT);
}
```

```java
@Profile(description = "...")
public class SpecXxxProfileDefinition {

    @Parameter(order = 1, required = false)
    public Param<SpecDecisionTreeStatus> finalStatus() { return Param.allValues(); }

    @DefaultConfig(description = "Config A")
    public SpecXxxProfileConfig configA() {
        return SpecXxxProfileConfig.create().setFinalStatus(statuses::RETURNS_C_OK_380);
    }
}
```

Classes **générées** par `kis-engine-core-annotation-processor` (ne jamais les écrire) :
`SpecXxxProfile` (une constante par `@DefaultConfig`), `SpecXxxProfileConfig`, `SpecXxxRule` (une
constante par `@InitRule`), `SpecXxxRuleFactory`, plus les `FactoryMachine`.

### Trois contraintes du socle, vérifiées dans `kis-engine-core`

**1. `otherwise` est obligatoire — et vérifié au démarrage de l'application.**
`RuleDatabaseInitializer.ensureOtherwisePresentInStandardRule()` lève une `IllegalStateException`
si un `@InitRule` n'en a pas, et sa valeur doit désigner un `@DefaultConfig` existant.
Conséquence directe : **on ne peut pas livrer une règle avec zéro profil par défaut.**

**2. La règle saisie par le client passe avant celle du code.**
`DefaultEvaluableBusinessRule.evaluate()` évalue d'abord la règle utilisateur et ne retombe sur la
règle standard (le `@InitRule`) que si elle ne renvoie rien. Vider l'arbre livré n'enlève donc
aucune capacité de paramétrage.

**3. Les constantes sont générées depuis les noms de méthodes.**
`configA()` produit `SpecXxxProfile.CONFIG_A`. Supprimer ou renommer un `@DefaultConfig` fait
disparaître la constante et **casse la compilation de tout `@InitRule` qui la référence** — et le
nom de méthode est aussi une clé i18n. Les trois endroits bougent ensemble : profil, règle, i18n.

### Le motif « aucune configuration livrée »

Quand le client veut saisir lui-même toute la matrice et qu'on ne doit livrer aucune valeur, la
contrainte 1 impose de conserver **un** `@DefaultConfig` — vide :

```java
/** Deliberately empty: it is the "nothing configured" outcome, not a usable configuration. */
@DefaultConfig(description = "Bypass not configured")
public SpecXxxProfileConfig notConfigured() {
    return SpecXxxProfileConfig.create();
}
```

```java
public Expressions ruleInit = Expressions.of()
        .otherwise(SpecXxxProfile.NOT_CONFIGURED);
```

L'action appelante doit alors **refuser explicitement** plutôt que de continuer sans valeur :

```java
SpecDecisionTreeStatus finalStatus = profile.getFinalStatus()
        .orElseThrow(() -> new BusinessException(localizedMessageService.getLocalizedMessageOf(
                "No bypass control status configured for stock owner {0} and receipt type {1}",
                refStockOwner.getCode(), receiptType.getId())));
```

Ce message devient le comportement nominal tant que le client n'a pas paramétré : il doit être
traduit et nommer le couple manquant (voir §12).

### Évaluation et test

Depuis une action, via la factory générée :

```java
SpecXxxProfile profile = specXxxRuleFactory.create(SpecXxxRule.SPEC_XXX).evaluate(wmsReceptionFolder);
```

En test, on compile l'arbre sans base ni contexte Studio (modèle `SpecBypassRuleTest`) :

```java
Expressions expressions = new SpecXxxRuleDefinition().ruleInit;
Rule<String> rule = expressions.transformUsing(ruleCompiler);   // GroovyRuleCompiler
```

Le test porte sur les **clés de configuration** renvoyées, pas sur les valeurs métier. Utile aussi :
`expressions.getExpressions()` (les branches) et `expressions.getOtherwise()` — de quoi verrouiller
qu'une règle livrée vide l'est restée, et qu'elle a bien son `otherwise`.

### Déclaration

Profil et règle sont **deux composants distincts** du `kis.descriptor.yaml` du module `-rule`, et
la règle dépend du profil :

```yaml
  - name: xxx/spec-wms-goodsin-profile-<nom>
    type: profile
  - name: xxx/spec-wms-goodsin-rule-<nom>
    type: rule
    dependencies:
      - xxx/spec-wms-goodsin-profile-<nom>@<version>
```

Ni l'un ni l'autre n'a besoin d'une ligne dans `bundle.list` : ils arrivent transitivement par la
dépendance du pom de `xxx-odatio-app` vers le module `-rule` (voir §10). L'action qui évalue la
règle, elle, en a besoin, et doit déclarer la règle dans ses `dependencies`.

L'écran de paramétrage des paramètres du profil est un composant UI à part
(`xxx/spec-wms-goodsin-ui-profile-<nom>`, type `ui-plugin`), qui lui **exige** sa ligne dans
`bundle.list`.

---

## 7. REST

Module `-rest`, un `@RestResource` par entité exposée. Modèle : `SpecWmsGoodsinTemporaryHuUiRest`.

```java
@RestResource
public class SpecXxxUiRest {

    @UniverseRequired
    @Transactional(readOnly = true, filterable = true)
    @PermissionsRequired(RETURNS_PERMISSION)
    @GET("/spec/wms/goodsin/xxxs")
    public List<SpecXxx> findXxxs(String filter, String sort, Integer limit, Integer offset, String timezone) {
        return specXxxRepository.find(params.of(filter, sort, limit, offset, ZoneId.of(timezone)));
    }

    @Transactional(readOnly = true, filterable = true)
    @PermissionsRequired(RETURNS_PERMISSION)
    @GET("/spec/wms/goodsin/xxxs/count")
    public Long countXxxs(String filter, String timezone) {
        return specXxxRepository.count(params.of(filter, ZoneId.of(timezone)));
    }
}
```

- **Une grille a toujours besoin des deux endpoints** : la liste et le `/count`.
- `@UniverseRequired` sur la liste, pas sur le count.
- Permission : réutiliser une permission existante (`goodsin:spec-returns`, constante
  `SpecWmsGoodsInReturnsUiRest.RETURNS_PERMISSION`) sauf besoin explicite d'en créer une.

---

## 8. Écran UI

Dépôt `odatio-ui-xxx`. Modèle : `xxx-spec-wms-goodsin-ui/spec-wms-goodsin-ui-temporaryhu`.

Arborescence d'un composant :

```
spec-<domaine>-ui-<nom>/
├── resources/
│   ├── i18n.fr_FR.txt
│   └── i18n.en_GB.txt
└── src/
    ├── controllers/spec-<domaine>-ui-<nom>-list.js      (+ -detail.js si vue détail)
    ├── list/spec-<nom>-list.html
    └── detail/spec-<nom>-detail.html                    (si vue détail)
```

- **Liste seule** si l'entité est plate. La vue `kis-ui-rich-detail` ne se justifie que si l'entité
  a une collection fille à afficher (cas `temporaryhu` vers `temporaryhudetail`).
- Le contrôleur déclare le datasource sur l'URL REST et l'univers :
  `datasourceFactory.createFilterable("/spec/wms/goodsin/xxxs", "GoodsInUniverse")`.
- Chaque grille a un identifiant de binding court et unique (`swgtemphu-mainGrid`, `swgrba-mainGrid`) :
  il sert à persister les préférences utilisateur, il ne doit pas entrer en collision.
- Colonnes `id` et `version` toujours `exclude`. La clé métier porte `identifier important`.
- Les libellés de colonnes viennent de l'i18n du **datamodel** (`specXxx.champA`), pas de celui de l'UI.
  L'i18n de l'UI ne porte que les libellés d'écran (titre, sections).

---

## 9. `kis.descriptor.yaml`

Chaque module déclare ses composants. **Un composant non déclaré n'existe pas au runtime.**

Types utilisés : `datamodel`, `datarepository`, `library` (module core), `action`, `rule`,
`profile`, `rest-resource`, `ui-plugin`.

Dans les exemples, `<version>` désigne la **version courante du projet spécifique** : la reprendre
d'un composant voisin du même descripteur ou du `bundle.list`, jamais d'un exemple figé.

Back — une entrée par action :

```yaml
  - name: xxx/spec-wms-goodsin-action-<nom>
    version: <version>
    type: action
    description: 'SPECIFIC <description>'
    dependencies:
      - a-sis/kis-engine-core@3.12-SNAPSHOT
    compile-dependencies:
      - a-sis/kis-engine-core-annotation-processor@3.12-SNAPSHOT
    test-dependencies:
      - a-sis/kis-base-tests@3.12-SNAPSHOT
```

UI — le `pluginDef` porte les routes et l'entrée de menu :

```yaml
  - name: xxx/spec-wms-goodsin-ui-<nom>
    version: <version>
    type: ui-plugin
    description: '...'
    dependencies:
      - a-sis/kis-ui-filterable-grid@3.12-SNAPSHOT
      - a-sis/kis-ui-services@3.12-SNAPSHOT
      - a-sis/kis-ui-socle-diplodo@3.12-SNAPSHOT
      - xxx/spec-wms-goodsin-rest-api@<version>
    properties:
        externalDependencies:
          - ui.bootstrap
        pluginDef:
          '{
            "contributions" : {
              "routes" : [ {
                "path" : "/spec-wms-goodsin-ui-<nom>",
                "permissions" : [ "goodsin:spec-returns" ],
                "config" : { "templateUrl" : "list/spec-<nom>-list.html" }
              } ],
              "workspaces" : [ {
                "shortLabel" : "...",
                "longLabel" : "...",
                "categoryId" : "reception",
                "subcategoryId" : "operational",
                "position" : 220,
                "entryPath" : "/spec-wms-goodsin-ui-<nom>"
              } ]
            }
          }'
```

`shortLabel` et `longLabel` sont des **clés de traduction** : elles doivent figurer dans
`resources/i18n.fr_FR.txt` et `i18n.en_GB.txt` du composant.
`position` ordonne le menu — prendre la valeur suivante libre (200, 210, 220…).

### 9.1 Les dépendances : deux fichiers à tenir, jamais un seul

Utiliser une classe qu'on n'importait pas encore dans un module oblige à **deux** déclarations :

| Fichier | Sert à | Symptôme si oublié |
|---|---|---|
| `pom.xml` du module | compiler et lancer les tests | erreur de compilation, immédiate |
| `kis.descriptor.yaml`, bloc du composant | charger la classe **au runtime** | `NoClassDefFoundError` / composant en erreur au démarrage, **rien au build** |

C'est l'asymétrie dangereuse : avec le pom seul, `mvn install` est vert, les tests passent, et le
défaut ne sort qu'au démarrage de l'appli — voire seulement quand le processus Studio appelle
l'action. Réflexe : après avoir ajouté un `import`, ouvrir le `kis.descriptor.yaml`.

Répartition des trois listes :

- `dependencies` : ce qu'importe le code de `src/main` (datamodel, datarepository, `util-exception`
  dès qu'on lève une `BusinessException` ou qu'on injecte `LocalizedMessageService`…).
- `compile-dependencies` : uniquement les processeurs d'annotations
  (`kis-engine-core-annotation-processor` pour une action/règle,
  `kis-persistence-core-annotation-processor` pour un datamodel/datarepository).
- `test-dependencies` : ce qu'importe `src/test` — `kis-base-tests`, `kis-persistence-tests`
  (pour `DataRepositoryRule`), `kis-engine-core-tests`, et **chaque** générateur
  `util-test-datamodel-*` utilisé.

### 9.2 Un nom de composant KIS n'est pas un artifactId Maven

Le descripteur se référence en **noms de composants KIS**, dont la granularité est plus fine que
celle des artefacts Maven. Un même artefact publie souvent plusieurs composants :

```
pom.xml                 →  a-sis:util-test-datamodel-wms
kis.descriptor.yaml     →  a-sis/util-test-datamodel-wms-goodsin
                           a-sis/util-test-datamodel-wms-cartography
                           a-sis/util-test-datamodel-wms-content   … (6 composants)
```

Même découpage pour `util-test-datamodel-ref` (`-ref-organization`, `-ref-cartography`,
`-ref-product`…). Pour trouver le nom exact à écrire, lire le descripteur embarqué dans le jar :

```bash
unzip -p ~/.m2/repository/a-sis/util-test-datamodel-wms/26.14-SNAPSHOT/util-test-datamodel-wms-26.14-SNAPSHOT.jar kis.descriptor.yaml | grep "  - name:"
```

### 9.3 Les dépendances sont par composant, pas par module

Dans un descripteur multi-composants (`-action` en déclare une vingtaine, `-rule` en déclare huit),
chaque bloc porte ses propres listes. Ajouter une dépendance au module ne la donne pas aux autres
composants : c'est **le bloc de l'action ou de la règle concernée** qu'il faut compléter.

Contrôle rapide de l'exhaustivité d'un module :

```bash
grep -rhn "^import static com.asis.util.test" <module>/src/test --include=*.java \
  | sed 's/.*com.asis.util.test/com.asis.util.test/' | cut -d. -f1-8 | sort -u
```

et confronter le résultat aux `test-dependencies` du bloc. Même principe avec les `import` de
`src/main` pour `dependencies`.

*Dette connue* : le bloc `xxx/spec-wms-goodsin-datarepository` ne déclarait aucun générateur de
test alors que ses tests en utilisaient quatre (corrigé). Les blocs du module `-action` sont, eux,
exacts : ils servent de modèle.

---

## 10. `bundle.list`

`xxx-odatio-app/src/main/resources/xxx/odatio/app/bundle.list`

C'est la **liste de chargement au démarrage** : une ligne `runtime:<composant>@<version>` par
composant à charger. Un composant déclaré dans un `kis.descriptor.yaml` mais absent d'ici existe au
packaging et **n'est jamais chargé** — l'erreur est tardive et muette côté build : l'action est
simplement « introuvable » au moment où le processus Studio l'appelle.

Le fichier est organisé en sections commentées : ranger la nouvelle ligne dans la bonne section,
à côté de ses pairs.

| Section | Contenu |
|---|---|
| `## editor ##` | les actions, les plugins UI de profil, et les composants globaux de domaine (`-core`, `-datamodel`, `-datarepository`, `-rest-api`) |
| `## UI ##` | les écrans (plugins UI avec entrée de menu) |
| `## FORK ##` | les forks du standard (`xxx/wms-goodsin-core`…) |
| `## integrator ##` | les composants d'intégration |
| `## spec-util ##` | les utilitaires spécifiques (initializers…) |

Qui a besoin d'une ligne :

| Composant | Ligne dans `bundle.list` ? |
|---|---|
| action | **oui**, toujours |
| plugin UI (écran, profil UI) | **oui**, toujours |
| entité / repository / ressource REST | pas de ligne par entité : la ligne du composant global du domaine (`spec-<domaine>-datamodel`, `-datarepository`, `-rest-api`, `-core`) suffit. **Pour un nouveau domaine, ces lignes sont à créer** |
| règle, profil | non — tirés **transitivement** |

*Incohérence connue* : les règles `spec-wms-content-rule-*` ont une ligne dans `## editor ##`,
celles de goodsin n'en ont pas. Les deux fonctionnent ; ne pas prendre les lignes de content comme
la preuve qu'une règle en exige une.

Le cas « transitif » mérite une vigilance particulière : une règle et son profil n'arrivent au
runtime que parce que `xxx-odatio-app/pom.xml` déclare une dépendance vers le module qui les
porte (`spec-wms-goodsin-rule`). Si on crée une règle dans un module **nouveau**, il ne suffit pas
de la déclarer dans son descripteur : il faut ajouter la dépendance Maven dans le pom de l'app, ou
lui donner sa propre ligne. Deux fichiers à regarder, pas un.

Vérification après build de l'app :

```bash
grep -n "<nom-du-composant>" xxx-odatio-app/src/main/resources/xxx/odatio/app/bundle.list
```

---

## 11. Traces

Préfixe **`SPEC - `** sur tout message de log spécifique. C'est ce qui permet de filtrer le
spécifique dans `app.log` au milieu du standard.

Ce préfixe est **réservé aux logs**. Il ne doit jamais apparaître dans un message métier destiné
à l'opérateur (voir §12) : l'opérateur n'a pas à savoir qu'il est dans du spécifique.

```java
if (logger.isInfoEnabled()) {
    logger.info("SPEC - Box replenishment - Create logistic orders for {} at {}",
            wmsHandlingUnit.toStringBusiness(), wmsLocation.toStringBusiness());
}
```

Règles :

- **Toujours** garder l'appel derrière `if (logger.isInfoEnabled())` / `isDebugEnabled()`.
- Placeholders `{}` (SLF4J), jamais de concaténation.
- Pour les entités, utiliser `toStringBusiness()` (lisible) ou `toStringNaturalIds()`.
- Sous-préfixer par fonctionnalité quand le volume le justifie :
  `SPEC - Box replenishment - ...`, `SPEC - Load box product - ...`.
- Tracer les décisions, pas les entrées de méthode : « pourquoi rien n'a été créé » est
  l'information utile en exploitation.

---

## 12. Messages métier et traduction

Les exceptions métier passent par `LocalizedMessageService` :

```java
throw new BusinessException(localizedMessageService.getLocalizedMessageOf(
        "Location {0} already has contents to repack", wmsLocation.toStringNaturalIds()));
```

- **Pas de préfixe `SPEC - `.** Il est réservé aux logs (§11). Un message métier est lu par
  l'opérateur, qui n'a pas à voir « SPEC ». Ni la clé, ni la valeur ne le portent.
- **Placeholders `{0}`, `{1}` (MessageFormat), pas `{}`.** C'est l'erreur la plus fréquente :
  un message écrit avec `{}` ne s'interpole pas et ne se traduit pas.
- La chaîne anglaise du code est la **clé** du fichier i18n.
- Chaque message doit être ajouté dans **les deux** fichiers, à côté du composant qui le lève :
  - action : `src/main/resources/xxx/spec/<domaine>/action/<chemin>/i18n.*.txt`
  - service core : `src/main/resources/xxx/spec/<domaine>/core/i18n.*.txt`
- Format `clé:valeur`, une par ligne.

```
Location {0} already has contents to repack:L'emplacement {0} a déjà des contenus à reconditionner
```

**Les apostrophes ne posent pas de problème.** `TranslationService.getEscapedMessage()` double
automatiquement toute apostrophe isolée avant de passer à `MessageFormat`
(`replaceAll("(?<!')'(?!')", "''")`). Une traduction française peut donc s'écrire naturellement —
`L'accord de retour {0} n'existe pas` — sans casser les placeholders et sans qu'il faille doubler
les apostrophes ni reformuler pour les éviter.

**Vérifier que le répertoire i18n de l'action existe.** Le pire cas n'est pas le message oublié
dans un fichier, c'est l'action qui n'a **aucun** fichier i18n : le message sort alors en anglais
brut, préfixe technique compris. Rencontré sur `action/load/bypasscontrolstatus`, dont le
répertoire `src/main/resources/.../bypasscontrolstatus/` n'existait pas. Réflexe après avoir écrit
un `getLocalizedMessageOf(...)` :

```bash
ls src/main/resources/xxx/spec/<domaine>/action/<chemin>/
```

**Relire les messages qui deviennent nominaux.** Un message conçu comme un garde-fou improbable
peut devenir le comportement courant après une évolution — par exemple « aucune configuration
trouvée » quand on décide de ne plus livrer de configuration par défaut. À ce moment-là il mérite
le même soin qu'un libellé d'écran : pas de préfixe `SPEC - `, traduction présente, formulation
qui nomme ce qui manque.

*Dettes connues* :

- 9 messages métier portent encore le préfixe `SPEC - ` dans leur clé, tous dans le périmètre
  reconditionnement / cartons : `SpecRepackingServiceImpl` (5), `SpecLoadRepackingLocationAction` (2),
  `SpecLoadBoxProductLocationAction` (1), `SpecCheckHandlingUnitCompatibleWithProductAction` (1).
  Les retirer implique de modifier la clé aux trois endroits à la fois (code + les deux i18n),
  sinon le message n'est plus traduit. À faire si on repasse sur ces composants.

---

## 13. Studio

Répertoire `studio/<nom-du-processus>/` :

| Fichier | Rôle |
|---|---|
| `customized.xml` | la définition exécutée |
| `customized.json` | le modèle de l'éditeur Studio (doit refléter le XML) |
| `i18n.fr_FR.txt` / `i18n.en_GB.txt` | libellés d'écran |

**Règle absolue** : `customized.json` doit rester cohérent avec `customized.xml`, et la clé
`xmlHash` du JSON doit valoir **le MD5 du XML normalisé LF** :

```bash
md5sum customized.xml    # doit être égal à la valeur de "xmlHash" dans customized.json
```

Après toute modification manuelle du XML : recalculer et réinjecter le hash.

Modifier le JSON à la main est risqué (structure de l'éditeur, métadonnées de diagramme).
**Préférer toujours passer par l'IHM Studio** ; en cas d'édition scriptée, vérifier ensuite que
les compteurs concordent avec le XML (nombre d'`interaction`, `action-ref`, `property`, `submit`,
`transition`, `variable`) et que le JSON parse.

Les libellés des `<property label="...">` et des `<interaction title="...">` sont des clés à
déclarer dans les deux fichiers i18n du processus, triées par ordre alphabétique
(insensible à la casse) comme le reste du fichier.

### 13.1 Contrôles après édition scriptée

Le jeu de vérifications qui a détecté toutes les erreurs rencontrées jusqu'ici :

```powershell
# XML et JSON parsent
[xml](Get-Content -Raw customized.xml)
$j = Get-Content -Raw customized.json | ConvertFrom-Json
"variables:$($j.variables.Count) interactions:$($j.interactions.Count) actions:$($j.actions.Count) subprocesses:$($j.subprocesses.Count) gateways:$($j.gateways.Count)"
```

```bash
# compteurs XML, à confronter au JSON :
#   variable ↔ variables | interaction ↔ interactions | action-ref ↔ actions
#   process-ref ↔ subprocesses | gateway ↔ gateways
for t in variable interaction action-ref process-ref gateway; do
  printf "%-12s %s\n" "$t" "$(grep -c "<$t" customized.xml)"
done

# toutes les cibles de transition existent (attention : les id peuvent contenir des espaces)
grep -oE '<transition to="[^"]*"|<transitions default="[^"]*"' customized.xml \
  | sed 's/.*="//;s/"//' | sort -u

# et enfin le hash
md5sum customized.xml
```

Le contrôle des cibles est celui qui rattrape les suppressions : retirer une interaction sans
retirer la transition qui y menait laisse un processus qui casse au runtime, pas au build.
À l'inverse, chercher les `id` jamais ciblés détecte les nœuds devenus orphelins — le seul
orphelin légitime est le point d'entrée du processus.

Ne pas construire ce contrôle sur `to="..."` tout court : l'attribut sert aussi aux mappings
`<input to=...>` / `<output to=...>`, et le résultat est noyé de faux positifs.

### 13.2 Le JSON peut être régénéré par Studio

Après un passage par l'IHM, le `customized.json` est réécrit : les clés `$$hashKey` (résidus
Angular) disparaissent, la mise en forme change. Une édition scriptée précédente n'est pas perdue
pour autant — mais avant de rééditer le JSON, **relire l'état courant** plutôt que de se fier au
diff qu'on croit connaître.

### 13.3 Modifier un message plutôt qu'ajouter une interaction

Pour enrichir un message d'erreur existant, préférer **rendre dynamique le message de
l'interaction déjà en place** plutôt qu'ajouter une interaction et une transition : cela évite
d'inventer des métadonnées de diagramme (`_diagramProps`, `vertex`, `routingMetaData`) et de
renuméroter les `position` des transitions dans le JSON.

```xml
<error translate="false">
  ${$ctx.xxxLocal != null
      ? $translate.message('Message précis {0} {1}', $ctx.aLocal, $ctx.bLocal)
      : $translate.message('Message générique de repli')}
</error>
```

Les expressions `${...}` acceptent les ternaires et les closures Groovy (il en existe déjà dans
les `visible=` et les `value=` des panneaux de progression). Penser `translate="false"` sur la
balise dès qu'on passe par `$translate.message(...)` soi-même.

---

## 14. Documentation fonctionnelle

Chaque action a un `.adoc` sous
`src/docs/functional/_includes/xxx/spec/<domaine>/action/<chemin>/<nom>-action.func.adoc` :

```asciidoc
[[spec-wms-goodsin-action-<nom>]]
== Titre en français
_spec-wms-goodsin-action-<nom>_

[NOTE]
====
Ce que fait l'action et pourquoi.
====

[cols="2,1,4"]
.Entrée(s)
|===
|Paramètre|Requis|Description
|===

[cols="2,5"]
.Sortie(s)
|===
|Paramètre|Description
|===
```

**À mettre à jour dès qu'on touche aux `@In` / `@Out` ou au comportement.** Plusieurs `.adoc`
étaient périmés (sorties disparues, entrées non documentées) : vérifier la cohérence avec la classe
avant de la considérer comme référence.

---

## 15. Tests

- JUnit 4 (`org.junit.Test`, `@Before`) + Mockito + AssertJ.
- Toutes les dépendances du service sont des `mock(...)` en champs, le service est instancié dans
  `@Before`. **Ajouter une dépendance à un service oblige à mettre à jour l'appel constructeur
  du test** — c'est la première chose qui casse.
- Nommage : `should_<comportement_attendu>_when_<condition>`.
- Utiliser les générateurs standard quand ils existent :
  `newRefSite()`, `newWmsLocation()`, `newWmsHandlingUnit()` (`com.asis.util.test.datamodel...`).
- Pour vérifier ce qui est persisté, `ArgumentCaptor` sur le `save()` du repository.
- Attention aux comparateurs du standard (`WmsOperationalOrderComparatorByMovementPathOrder`) :
  ils déréférencent la cartographie et lèvent des NPE sur des mocks nus. Contournement : faire
  renvoyer une collection d'ordres opérationnels vide par l'ordre logistique mocké quand on ne
  teste pas le tri.
- Un mock de repository qui renvoie un `Optional` doit être **stubbé dès le `@Before`**
  (`thenReturn(Optional.empty())`) : sans stub Mockito renvoie `null`, et le premier
  `.ifPresent(...)` part en NPE.

### 15.1 Tests d'intégration de repository (`DataRepositoryRule`)

Un test de repository tourne sur une vraie base Derby. Modèle : `SpecWmsGoodsInFolderRepositoryTest`.

```java
@Rule
public DataRepositoryRule<SpecXxxRepository> repositoryRule =
        DataRepositoryRule.defaultFor(sessionFactory -> new SpecXxxRepository(sessionFactory, ...));

@Test
public void should_... () {
    repositoryRule.doInTransaction(session -> {
        newWmsGoodsInFolder().code("GIF1").location(location).insert(session);
        session.flush();
        assertThat(repositoryRule.get().specFindXxx(...)).isPresent();
    });
}
```

Les générateurs `insert(session)` construisent tout le graphe de dépendances manquant (site,
emplacement…) : inutile de le monter à la main.

**Le piège des énumérés dynamiques.** `MockEnumerable.mockEnum(...)` ne sait pas **ordonner** les
énumérés : une requête qui compare un statut (`status().lt(...)`, `.gt(...)`, `.goe(...)`) ne
remonte alors **jamais rien**, alors que la requête est juste. Il faut
`MockEnumerable.mockEnumInDatabase(EnumClass.Enums.class, sessionFactory)`, construit dans la
lambda de la règle et **conservé dans un champ** pour être partagé avec les entités insérées :

```java
private WmsGoodsInFolder.Status.Enums statuses;

@Rule
public DataRepositoryRule<SpecWmsGoodsInFolderRepository> repositoryRule =
        DataRepositoryRule.defaultFor(sessionFactory -> {
            statuses = mockEnumInDatabase(WmsGoodsInFolder.Status.Enums.class, sessionFactory);
            return new SpecWmsGoodsInFolderRepository(sessionFactory, statuses, mock(TranslationService.class));
        });
```

Deux instances distinctes ne produisent pas les mêmes identifiants, la comparaison échoue à
nouveau. `mockEnum` reste suffisant pour les tests mockés (action, service) et pour les requêtes
qui ne comparent que des égalités.

Ce piège est silencieux et pousse à la mauvaise correction (retirer le filtre de statut pour faire
passer le test, et livrer une requête fausse). Devant un test de repository vide contre toute
attente, **bissecter la clause `where`** avant de douter de la requête.

Coût : ~25 s par test (création du schéma). Ne pas multiplier les cas.

---

## 16. Build

```bash
# back — fonctionne hors ligne
cd C:/Odatio/feat_xxx_SPEC/odatio-xxx
mvn -o install                             # tout, avec tests (~10 min)
mvn -o install -DskipTests                 # compilation seule
mvn -o -pl <module> test -Dtest=<Classe>   # un test : lancer depuis odatio-xxx, sinon -pl ne résout pas
mvn -o -pl <module> test "-Dtest=<Classe>#<methode>"   # une seule méthode (utile pour bissecter)

# UI — nécessite le réseau (ui-builder-maven-plugin absent du repo local)
cd C:/Odatio/feat_xxx_SPEC/odatio-ui-xxx
mvn install

# assemblage
cd C:/Odatio/feat_xxx_SPEC/xxx-odatio-app
mvn install
```

Vérifier après build UI que le composant est bien packagé :

```bash
unzip -l xxx-spec-wms-goodsin-ui/target/spec-wms-goodsin-ui-<version>.zip | grep <nom>
```

Le `.pack.js` du composant doit apparaître à côté du HTML et des i18n.

---

## 17. Checklist — nouvelle entité exposée dans un écran

1. `SpecXxx.java` dans `-datamodel` (avec `@Universe`, `@NaturalId`, `generateId()`)
2. `V<n>-*.sql` sur les 4 SGBD
3. i18n entité (fr + en) dans les ressources du `-datamodel`
4. `SpecXxxRepository` dans `-datarepository`
5. Le service / l'action qui l'alimente, avec traces `SPEC - `
6. Entrée de l'action (ou de la règle) dans le `kis.descriptor.yaml` **back**, avec ses
   `dependencies` / `compile-dependencies` / `test-dependencies` (§9, §9.1)
7. Messages métier ajoutés aux deux i18n du module concerné, en `{0}`
8. `SpecXxxUiRest` : endpoints liste **et** count
9. Composant UI (liste HTML + contrôleur + i18n écran)
10. Entrée `ui-plugin` dans le `kis.descriptor.yaml` UI (route + workspace + position)
11. Ligne dans `bundle.list`, bonne section
12. `.adoc` des actions créées ou modifiées
13. Tests (service + constructeur des tests existants à mettre à jour)
14. Build back + UI + app
15. **Jouer le script SQL sur la base avant de démarrer**

## 17bis. Checklist — modifier un composant existant

C'est le cas le plus fréquent (correctif, ajustement IHM), et celui où les oublis coûtent le plus
cher parce que le build reste vert.

1. **Nouveaux `import` ?** → `dependencies` / `test-dependencies` du bloc du composant dans
   `kis.descriptor.yaml` (§9.1, §9.3) — en noms de composants KIS, pas d'artefacts Maven (§9.2)
2. **Nouveau `@In` / `@Out` ?** → jamais renommer un existant ; mettre à jour le `.adoc` (§14)
3. **Nouveau champ sur une entité existante ?** → quatre choses vont ensemble, et le build reste
   vert si l'une manque : la clé `specXxx.champ` dans les **deux** i18n du `-datamodel` (§3.4),
   le script `V<n>` de migration sur les **4** SGBD (§3.3), la colonne dans le template de grille
   (§8), et le constructeur de l'entité — donc tous ses appelants et leurs tests. Sans la clé i18n,
   la grille affiche le nom technique du champ ; c'est l'oubli le plus fréquent parce qu'il ne se
   voit qu'à l'écran, en recette.
4. **Nouveau message métier ?** → les deux i18n, en `{0}`, sans préfixe `SPEC - `, et
   **vérifier que le répertoire i18n existe** (§12)
5. **Message ou clé supprimés ?** → retirer les clés mortes des deux i18n, après avoir vérifié
   qu'aucun autre composant ne les utilise
6. **Processus Studio touché ?** → `customized.json` aligné, `xmlHash` recalculé, compteurs et
   cibles de transition vérifiés (§13, §13.1)
7. **Nouveau composant (action, plugin UI) ?** → ligne dans `bundle.list` (§10)
8. **Un `throw` supprimé ou déplacé ?** → relire le flot en aval, qui n'était jamais atteint
9. **Tests existants** : un constructeur qui gagne un paramètre casse tous les tests du service ;
   un mock qui renvoie un `Optional` doit être stubbé (§15)
10. Build du module, puis `mvn -o install` sur `odatio-xxx`, puis `xxx-odatio-app`
11. Restitution Jira avec les **résidus assumés** et ce qui reste **à valider en recette** (§19)

---

## 18. Pièges rencontrés

- **Champs custom du standard** : `WmsLogisticOrder` hérite de
  `AuditableCustomizableVersionableLockableUUIDEntity`, d'où `customString1..5`, `customLong1..5`,
  `customDate1..5`, `customText`. Avant d'en réserver un, vérifier qu'il n'est pas déjà pris
  (`grep -rn "customStringN"`), et lui donner un accesseur nommé dans le repository spécifique.
- **Filtre strict sur des données existantes** : ajouter un critère sur un champ custom rend
  invisibles les enregistrements créés avant la livraison. Prévoir une fenêtre de déploiement sans
  flux en cours, ou une reprise de données — ne pas « assouplir » le filtre, cela annule la garantie
  qu'on cherchait à obtenir.
- **`mvn -o` hors du bon répertoire** : `-pl` ne résout pas les modules et le build échoue en
  indiquant un module sans rapport.
- **`app.log`** : les `ERROR` de `LogAlertMessages` (timeouts de traduction au démarrage) et les
  `WARN HibernateKryoRegister` (sérialisation d'ordres opérationnels) sont du bruit habituel,
  pas des régressions.
- **Le pom suffit à compiler, pas à démarrer** : une dépendance ajoutée au `pom.xml` mais pas au
  `kis.descriptor.yaml` du composant donne un build vert et un défaut au runtime (§9.1).
- **Message d'erreur statique** : un `<error>` de processus Studio sans paramètre ne pourra jamais
  nommer le cas rencontré. Quand un message est jugé trop vague, regarder d'abord s'il est
  paramétrable avant de chercher un défaut de logique — la condition de déclenchement est souvent
  la bonne (§13.3).
- **Supprimer un `throw` change le flot d'après** : remplacer une exception par une sortie
  « information » oblige à relire ce que fait le code en aval, qui n'était jamais atteint. Cas
  rencontré sur `SpecLoadGoodsInTemporaryHuAction` : retirer le contrôle de quai laissait reprendre
  l'arrivage d'un autre quai, il a fallu ajouter un `filter` explicite.
- **Test de repository vide contre toute attente** : bissecter la clause `where` avant de douter de
  la requête ; c'est souvent `mockEnum` qui n'ordonne pas les statuts (§15.1). Ne jamais retirer le
  filtre pour faire passer le test.
- **Supprimer un `@DefaultConfig`** casse la compilation de la règle qui le référence, et une règle
  sans `otherwise` empêche l'application de démarrer (§6bis).
- **Clés i18n devenues mortes** : après suppression d'une interaction ou d'un `@DefaultConfig`,
  retirer les clés correspondantes des **deux** fichiers, et vérifier qu'un libellé partagé
  (`OK`, `Date limit returns allowed`) n'est pas encore utilisé ailleurs avant de l'enlever.
  Contrôle : `diff <(cut -d: -f1 i18n.fr_FR.txt) <(cut -d: -f1 i18n.en_GB.txt)`.

---

## 19. Restitution Jira

Toute analyse d'incident ou livraison de correctif/évolution se restitue dans un **fichier HTML
autonome collable dans Jira**, à la racine de `feat_xxx_SPEC`, nommé `<sujet>-jira.html`
(`evolution2-bypass-role-jira.html`, `arrivages-chariots-bloques-jira.html`…).

Structure imposée : une **partie fonctionnelle** puis une **partie technique**. La partie
fonctionnelle est lisible par quelqu'un qui ne connaît pas le code — symptôme constaté, puis
comportement après correctif. La partie technique porte le détail : extraits de code
avant / après, points d'appel, fichiers modifiés, tests et build.

Rester **concis** : c'est un ticket, pas une documentation. Un tableau de synthèse en tête
(symptôme → statut) évite au lecteur de tout parcourir.

Contraintes de forme, imposées par le collage dans Jira :

- HTML **autonome**, `<style>` inline dans le `<head>`, aucune ressource externe.
- Balises simples uniquement (`h1`-`h3`, `p`, `ul`, `table`, `pre`, `code`, `strong`) : Jira
  ignore le reste au collage.
- Palette Atlassian pour que le rendu reste cohérent : titres `#0747a6`, texte `#172b4d`,
  fonds de code `#f4f5f7`, bordures `#dfe1e6`.
- Classes utilitaires du modèle : `.tag` (pastille `UI` / `Java` sur les sous-titres techniques),
  `.ok` (vert, statuts et builds), `.note` (encadré ambre), `.files` (tableau de fichiers).

Le plus simple est de **copier le squelette d'un fichier `-jira.html` existant** et d'en remplacer
le contenu.

**Ce qui est attendu explicitement** : les **points restants ouverts** et les **résidus assumés**
(ce que le correctif ne couvre pas, et pourquoi) apparaissent dans la partie fonctionnelle, en
encadré `.note`. C'est l'information que le relecteur du ticket ne peut pas déduire du code.


---

## 20. Commentaires

Ne mettre de commentaire que lorsqu'il est nécessaire, et le rendre aussi précis et concis que
possible. Les commentaires (Javadoc comprise) sont rédigés **en anglais**.
