# Employee Attendance & Analytics API

A FastAPI + MongoDB backend for employee records, attendance punch-in/punch-out, attendance regularization with an audit trail, and workforce analytics. The API follows the supplied HROne engineering-assignment contract (business rules R1–R10).

## Live deployment

- **Live Swagger UI:** http://hrone-attendance-api-env.eba-xdrej3yr.eu-north-1.elasticbeanstalk.com/docs
- **Health endpoint:** http://hrone-attendance-api-env.eba-xdrej3yr.eu-north-1.elasticbeanstalk.com/health
- **GitHub repository:** https://github.com/iamvrvasanth/hrone-attendance-analytics-api

The service is deployed to AWS Elastic Beanstalk in `eu-north-1`. GitHub is connected to AWS CodePipeline; a pipeline execution retrieves the source, runs the CodeBuild build/test stage, and deploys the application to Elastic Beanstalk. The last verified execution showed Source, Build_and_Deploy, and Deploy_HRONE succeeded. The environment health was `Ok`, and the Swagger UI loaded successfully. Employee creation/listing and attendance operations were tested against the live API.

## Features

- Employee creation and paginated employee listing.
- Race-safe attendance punch-in and punch-out, including overnight shifts.
- Attendance listing with filters and pagination.
- Manual attendance regularization with an append-only correction history.
- Monthly employee summaries and department summaries.
- Late-comer leaderboard with competition ranking and correct tie handling.
- Department daily trend with gap filling and a 7-day moving average.
- MongoDB execution-plan endpoint for checking index use.
- Idempotent startup index creation.

## Technology

- Python 3.11+
- FastAPI and Uvicorn
- PyMongo
- MongoDB 6.0+ (MongoDB 7.0 used by the assignment grader)
- AWS CodePipeline, CodeBuild, and Elastic Beanstalk

## Run locally

From the repository root:

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

Set the database environment variables. Do not commit a `.env` file or real credentials.

```bash
# macOS/Linux
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'

# Windows PowerShell
# $env:MONGO_URI = 'mongodb://localhost:27017'
# $env:MONGO_DB = 'attendance_db'
```

For a local MongoDB instance, Docker may be used for the database only:

```bash
docker run --rm --name hrone-mongo -p 27017:27017 mongo:7
```

If using the candidate kit's sample data, seed it using the provided script when available:

```bash
python sample_seed.py
```

Start the API with the required command:

```bash
uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000/docs for Swagger UI. The application reads `MONGO_URI` and `MONGO_DB` from the environment; defaults should only be used for local development.

## API overview

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Database readiness check |
| `POST` | `/employees` | Create an employee |
| `GET` | `/employees` | Filter and paginate employees |
| `POST` | `/attendance/punch-in` | Record punch-in |
| `POST` | `/attendance/punch-out` | Record punch-out and calculate derived values |
| `GET` | `/attendance` | Filter and paginate attendance records |
| `PATCH` | `/attendance/{emp_code}/{date}` | Correct a record and append audit history |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Employee monthly analytics |
| `GET` | `/analytics/departments/summary` | Monthly department analytics |
| `GET` | `/analytics/leaderboard/late` | Late-comer leaderboard |
| `GET` | `/analytics/departments/{department}/trend` | Daily department trend |
| `GET` | `/admin/explain/{endpoint}` | MongoDB execution plan and execution statistics |

See `/docs` and `openapi.yaml` for request schemas, parameters, and response details.

## Business rules and implementation notes

- Attendance dates and shift times use the contract's IST conventions; instants are epoch milliseconds at the API boundary and UTC BSON datetimes in MongoDB.
- MongoDB indexes are created at application startup.
- Analytics are implemented using MongoDB aggregation pipelines rather than pulling the full dataset into Python.
- Punch-in uniqueness is enforced by a compound unique index on employee and attendance date to protect against concurrent requests.
- Regularization history records manual corrections only.
- Keep MongoDB Atlas credentials in environment configuration or a secret manager. Never commit secrets, `.env`, a virtual environment, data dumps, or a Dockerfile to the submission repository.

## CI/CD

The GitHub repository is the pipeline source. AWS CodePipeline runs the CodeBuild build/test stage and then deploys the built application to the Elastic Beanstalk environment. Pushes to the configured source branch trigger the pipeline. Check the CodePipeline execution history for the status of each stage and the CodeBuild logs for test/build failures.

## Assignment submission checklist

- [x] `app/main.py` contains the application implementation.
- [x] `requirements.txt` declares runtime dependencies.
- [x] `README.md` documents the API, local run command, and live deployment.
- [x] `REVIEW.md` documents starter defects and fixes.
- [x] `DECISIONS.md` records index, concurrency, tie, headcount, and scaling decisions.
- [x] Repository is public: https://github.com/iamvrvasanth/hrone-attendance-analytics-api
- [x] Live Swagger UI: http://hrone-attendance-api-env.eba-xdrej3yr.eu-north-1.elasticbeanstalk.com/docs
- [ ] Before final submission, confirm `REVIEW.md` and `DECISIONS.md` are filled in the GitHub repository and verify that no `.env`, secrets, Dockerfile, virtual environment, or data dumps are committed.

## Security note

The API URL currently uses HTTP. For production use, configure HTTPS and appropriate authentication/authorization, review CORS, and restrict the CodePipeline role to least-privilege permissions. The current live deployment is a demonstration of the assignment API, not a claim that the service is production-hardened.
