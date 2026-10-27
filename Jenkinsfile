pipeline {
    agent any

    triggers {
        cron('TZ=Europe/Moscow\n0 6,12,17,20 * * *')
    }

    options {
        disableConcurrentBuilds()
        timeout(time: 30, unit: 'MINUTES')
    }

    environment {
        PYTHONUNBUFFERED = '1'
        REPORT_FILE = 'mts_gpon_report.json'
        PROXIES_ENABLED = 'true'
    }

    stages {
        stage('Tests') {
            steps {
                script {
                    if (isUnix()) {
                        sh 'python3 -m venv .venv'
                        sh '. .venv/bin/activate && python -m pip install --upgrade pip && pip install -r requirements.txt && playwright install chromium'
                    } else {
                        bat 'py -m venv .venv'
                        bat '.venv\\Scripts\\python.exe -m pip install --upgrade pip && .venv\\Scripts\\pip.exe install -r requirements.txt && .venv\\Scripts\\playwright.exe install chromium'
                    }
                }
                withCredentials([
                    string(credentialsId: 'mts_gpon_proxy_1_url', variable: 'PROXY_1_URL'),
                    string(credentialsId: 'mts_gpon_proxy_2_url', variable: 'PROXY_2_URL')
                ]) {
                    script {
                        if (isUnix()) {
                            sh '. .venv/bin/activate && python -m pytest -q tests && python checker.py'
                        } else {
                            bat '.venv\\Scripts\\python.exe -m pytest -q tests && .venv\\Scripts\\python.exe checker.py'
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'mts_gpon_report.json', allowEmptyArchive: true
        }
        failure {
            script {
                withCredentials([
                    string(credentialsId: 'telegram_proxy_url', variable: 'TELEGRAM_PROXY_URL'),
                    string(credentialsId: 'telegram_proxy_auth_secret', variable: 'TELEGRAM_PROXY_AUTH_SECRET'),
                    string(credentialsId: 'tg_proxy_creds_survarius', variable: 'TELEGRAM_PROXY_CREDS')
                ]) {
                    if (isUnix()) {
                        sh '. .venv/bin/activate && python notify.py || true'
                    } else {
                        bat '.venv\\Scripts\\python.exe notify.py'
                    }
                }
            }
        }
    }
}
