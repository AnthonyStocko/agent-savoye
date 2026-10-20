// Proposition de job dédié à la vérification des tests Cucumber générés par l'agent (onglet Cucumber).
// Aucun push : le patch du lot arrive en paramètre fichier, est appliqué sur une copie locale du dépôt Cucumber
// client, superposée au Cucumber Standard, puis seuls les scénarios tagués sont joués sur un environnement DÉJÀ
// démarré (pas de démarrage d'appli). Le rapport Cucumber JSON (et jacoco.exec si disponible) est archivé.
// Paramètres attendus par l'agent (vérifiés avant usage) : PATCH (fichier), TAGS, ENV.
// Nécessite le plugin « File Parameters » (stashedFile) et un credential par environnement (ENV -> URL, compte).
pipeline {
    agent { label 'maven' }
    parameters {
        stashedFile(name: 'PATCH', description: 'Patch git du lot (git diff depuis la base du dépôt Cucumber client)')
        string(name: 'TAGS', defaultValue: '', description: "Expression de tags Cucumber, ex. @agent_apparel_3_1 or @agent_apparel_3_2")
        string(name: 'ENV', defaultValue: '', description: 'Environnement cible déjà démarré (recette client ou Standard)')
        string(name: 'CUCUMBER_SPEC_REPOSITORY', defaultValue: 'apparel-cucumber', description: 'Dépôt Cucumber client')
        string(name: 'CUCUMBER_SPEC_BRANCH', defaultValue: 'integration', description: 'Branche de base du dépôt client')
        string(name: 'CUCUMBER_STANDARD_BRANCH', defaultValue: 'development', description: 'Branche du Cucumber Standard')
    }
    options { timeout(time: 60, unit: 'MINUTES') }
    stages {
        stage('Sources') {
            steps {
                dir('standard') { git url: 'https://git.kiscloud.io/scm/component-qa/cucumber.git', branch: params.CUCUMBER_STANDARD_BRANCH, credentialsId: 'bitbucket' }
                dir('spec') {
                    git url: "https://git.kiscloud.io/scm/${params.CUCUMBER_SPEC_REPOSITORY.split('-')[0]}/${params.CUCUMBER_SPEC_REPOSITORY}.git", branch: params.CUCUMBER_SPEC_BRANCH, credentialsId: 'bitbucket'
                    unstash 'PATCH'
                    sh 'git apply --whitespace=nowarn PATCH'
                }
                // Dépôt client en surcouche du Standard (comme la compilation locale de l'agent)
                sh 'cp -r spec/src/. standard/src/'
            }
        }
        stage('Cucumber') {
            steps {
                // Credential « cucumber-env-<ENV> » : URL (username) et mot de passe base64 (password) de l'environnement
                withCredentials([usernamePassword(credentialsId: "cucumber-env-${params.ENV}", usernameVariable: 'ENV_URL', passwordVariable: 'ENV_PASSWORD')]) {
                    dir('standard') {
                        sh '''mvn -B -q test "-Dcucumber.filter.tags=${TAGS}" -Dhttps=https -Durl="${ENV_URL}" -Dws=wss \
                              -DuserName=admin -Dpassword="${ENV_PASSWORD}" -Dmaven.test.failure.ignore=true'''
                    }
                }
            }
        }
    }
    post {
        always {
            archiveArtifacts allowEmptyArchive: true, artifacts: 'standard/target/cucumber-reports/*.json,**/jacoco*.exec'
        }
    }
}
