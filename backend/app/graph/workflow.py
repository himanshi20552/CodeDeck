try:
    from langgraph.graph import END, StateGraph
except ImportError:  # pragma: no cover
    # Minimal stubs so the module loads without langgraph
    class StateGraph:
        def __init__(self, *args, **kwargs):
            pass
        def add_node(self, *args, **kwargs):
            pass
        def set_entry_point(self, *args, **kwargs):
            pass
        def add_conditional_edges(self, *args, **kwargs):
            pass
        def add_edge(self, *args, **kwargs):
            pass
        def compile(self):
            return self
    END = 'END'

import logging
from pathlib import Path



from app.agents.docker_agent import generate_dockerfile
from app.agents.kubernetes_agent import generate_kubernetes
from app.agents.repository_agent import analyze_repository
from app.agents.security_agent import run_security_scan
from app.agents.testing_agent import run_tests
from app.agents.validate import validate_configuration, validate_dockerfile
from app.core.config import settings
from app.db.database import SessionLocal
from app.db.models import Analysis, GeneratedFile, Project
from app.graph.state import DeployMindState
from app.services.github import cleanup_repository, clone_repository
from app.services.deployment_contract import build_deployment_contract
from app.services.pipeline import upsert_step
from app.services.observability import measured
from app.services.repository_ai import (
    build_repository_index,
    evaluate_repository_index,
    generate_deployment_recommendation,
)

logger = logging.getLogger("deploymind")


def _save_analysis(project_id: int, analysis: dict, security_score: int | None = None) -> None:
    db = SessionLocal()
    try:
        row = db.query(Analysis).filter(Analysis.project_id == project_id).first()
        if not row:
            row = Analysis(project_id=project_id)
            db.add(row)
        row.language = analysis.get("language")
        row.framework = analysis.get("framework")
        row.package_manager = analysis.get("package_manager")
        row.entrypoint = analysis.get("entrypoint")
        row.test_command = analysis.get("test_command")
        row.has_dockerfile = bool(analysis.get("has_dockerfile"))
        if security_score is not None:
            row.security_score = security_score
        row.analysis_result = analysis
        db.commit()
    finally:
        db.close()


def _set_project_status(project_id: int, status: str) -> None:
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project:
            project.status = status
            db.commit()
    finally:
        db.close()


def _mark_step(project_id: int, name: str, status: str, result: str | None = None, error: str | None = None) -> None:
    db = SessionLocal()
    try:
        upsert_step(db, project_id, name, status, result=result, error=error)
    finally:
        db.close()


def _save_files(project_id: int, files: dict[str, str]) -> None:
    db = SessionLocal()
    try:
        db.query(GeneratedFile).filter(GeneratedFile.project_id == project_id).delete()
        for filename, content in files.items():
            db.add(GeneratedFile(project_id=project_id, filename=filename, content=content))
        db.commit()
    finally:
        db.close()

    out_dir = Path(settings.workspace_dir) / "generated" / f"project_{project_id}"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            (out_dir / filename).write_text(content, encoding="utf-8")
    except OSError:
        logger.warning("could not write generated files to %s", out_dir)


def clone_and_analyze(state: DeployMindState) -> DeployMindState:
    project_id = state["project_id"]
    url = state["repository_url"]
    dest = Path(settings.workspace_dir) / f"project_{project_id}"

    _mark_step(project_id, "Repository Analysis", "running")
    _set_project_status(project_id, "analyzing")
    errors = list(state.get("errors") or [])

    try:
        clone_repository(url, dest)
        result = analyze_repository(dest)
        analysis = result.model_dump()
        analysis["repository_name"] = state.get("repository_name") or dest.name

        if not result.supported:
            _mark_step(project_id, "Repository Analysis", "failed", error=result.message)
            _set_project_status(project_id, "failed")
            errors.append({"step": "repository_analysis", "message": result.message})
            return {
                **state,
                "repository_path": str(dest),
                "analysis": analysis,
                "errors": errors,
                "status": "failed",
            }

        _save_analysis(project_id, analysis)
        _mark_step(project_id, "Repository Analysis", "completed", result=result.message)
        logger.info("analysis completed for project %s", project_id)
        return {
            **state,
            "repository_path": str(dest),
            "analysis": analysis,
            "errors": errors,
            "status": "analyzed",
        }
    except Exception as exc:
        logger.exception("repository analysis failed")
        msg = str(exc)
        if settings.github_token:
            msg = msg.replace(settings.github_token, "***")
        _mark_step(project_id, "Repository Analysis", "failed", error=msg)
        _set_project_status(project_id, "failed")
        errors.append({"step": "repository_analysis", "message": msg})
        return {
            **state,
            "repository_path": str(dest),
            "errors": errors,
            "status": "failed",
        }


def run_tests_node(state: DeployMindState) -> DeployMindState:
    project_id = state["project_id"]
    repo_path = state.get("repository_path") or ""
    analysis = state.get("analysis") or {}
    errors = list(state.get("errors") or [])

    _mark_step(project_id, "Tests", "running")
    try:
        result = run_tests(repo_path, analysis)
        payload = result.model_dump()

        if result.status == "passed":
            _mark_step(project_id, "Tests", "completed", result=result.message)
        elif result.skipped:
            _mark_step(project_id, "Tests", "completed", result=result.message)
        else:
            _mark_step(project_id, "Tests", "failed", result=result.message, error=result.message)
            errors.append({"step": "tests", "message": result.message})

        merged = {**analysis, "test_result": payload}
        _save_analysis(project_id, merged)
        return {
            **state,
            "analysis": merged,
            "test_result": payload,
            "errors": errors,
            "status": "tests_done",
        }
    except Exception as exc:
        logger.exception("tests node failed")
        _mark_step(project_id, "Tests", "failed", error=str(exc))
        errors.append({"step": "tests", "message": str(exc)})
        return {**state, "errors": errors, "status": "tests_done"}


def repository_intelligence_node(state: DeployMindState) -> DeployMindState:
    project_id = state["project_id"]
    repo_path = state.get("repository_path") or ""
    analysis = dict(state.get("analysis") or {})
    errors = list(state.get("errors") or [])

    _mark_step(project_id, "Repository Intelligence", "running")
    try:
        metadata = build_repository_index(repo_path, project_id, state.get("repository_url") or "")
        analysis["repository_index"] = metadata.model_dump()
        recommendation = generate_deployment_recommendation(project_id, analysis)
        analysis["deployment_recommendation"] = recommendation.model_dump()
        evaluation = evaluate_repository_index(project_id, analysis)
        analysis["evaluation_result"] = evaluation.model_dump()
        _save_analysis(project_id, analysis)
        _mark_step(
            project_id,
            "Repository Intelligence",
            "completed",
            result=f"Indexed {metadata.file_count} files into {metadata.chunk_count} chunks",
        )
        return {**state, "analysis": analysis, "errors": errors, "status": "repository_intelligence_done"}
    except Exception as exc:
        logger.exception("repository intelligence node failed")
        msg = str(exc)
        errors.append({"step": "repository_intelligence", "message": msg})
        _mark_step(project_id, "Repository Intelligence", "warning", result="Repository intelligence skipped", error=msg)
        return {**state, "analysis": analysis, "errors": errors, "status": "repository_intelligence_done"}


def security_scan_node(state: DeployMindState) -> DeployMindState:
    project_id = state["project_id"]
    repo_path = state.get("repository_path") or ""
    analysis = dict(state.get("analysis") or {})
    errors = list(state.get("errors") or [])

    _mark_step(project_id, "Security", "running")
    try:
        result = run_security_scan(repo_path)
        payload = result.model_dump()
        analysis["security_result"] = payload

        step_status = "warning" if result.findings else "completed"
        _mark_step(project_id, "Security", step_status, result=result.summary)
        _save_analysis(project_id, analysis, security_score=result.score)

        return {
            **state,
            "analysis": analysis,
            "security_result": payload,
            "errors": errors,
            "status": "security_done",
        }
    except Exception as exc:
        logger.exception("security node failed")
        _mark_step(project_id, "Security", "failed", error=str(exc))
        errors.append({"step": "security", "message": str(exc)})
        return {**state, "errors": errors, "status": "security_done"}


def generate_docker_node(state: DeployMindState) -> DeployMindState:
    project_id = state["project_id"]
    repo_path = state.get("repository_path") or ""
    analysis = dict(state.get("analysis") or {})
    errors = list(state.get("errors") or [])

    _mark_step(project_id, "Docker", "running")
    try:
        result = generate_dockerfile(repo_path, analysis)
        docker_errors = validate_dockerfile(result.dockerfile)
        analysis["docker_result"] = {**result.model_dump(), "validation_errors": docker_errors}
        _save_analysis(project_id, analysis)

        if docker_errors:
            detail = "; ".join(docker_errors)
            _mark_step(project_id, "Docker", "failed", error=detail)
            errors.append({"step": "docker", "message": detail})
            return {**state, "analysis": analysis, "errors": errors, "status": "failed"}

        _save_files(project_id, {"Dockerfile": result.dockerfile})
        _mark_step(project_id, "Docker", "completed", result=result.message)
        return {
            **state,
            "analysis": analysis,
            "dockerfile": result.dockerfile,
            "errors": errors,
            "status": "docker_done",
        }
    except Exception as exc:
        logger.exception("docker node failed")
        _mark_step(project_id, "Docker", "failed", error=str(exc))
        errors.append({"step": "docker", "message": str(exc)})
        return {**state, "errors": errors, "status": "failed"}


def generate_kubernetes_node(state: DeployMindState) -> DeployMindState:
    project_id = state["project_id"]
    analysis = dict(state.get("analysis") or {})
    errors = list(state.get("errors") or [])
    dockerfile = state.get("dockerfile") or ""

    _mark_step(project_id, "Kubernetes", "running")
    try:
        result = generate_kubernetes(analysis, app_name=state.get("repository_name"))
        validation = validate_configuration(dockerfile, result.files)
        analysis["kubernetes_result"] = result.model_dump()
        analysis["validation_result"] = validation.model_dump()
        files = {"Dockerfile": dockerfile, **result.files}
        analysis["deployment_contract"] = build_deployment_contract(project_id, analysis, files).model_dump()
        _save_files(project_id, files)
        _save_analysis(project_id, analysis)

        if not validation.valid:
            detail = "; ".join(validation.errors[:5])
            _mark_step(project_id, "Kubernetes", "failed", error=detail)
            errors.append({"step": "kubernetes", "message": detail})
            _set_project_status(project_id, "failed")
            return {
                **state,
                "analysis": analysis,
                "kubernetes_files": result.files,
                "validation_result": validation.model_dump(),
                "errors": errors,
                "status": "failed",
            }

        _mark_step(project_id, "Kubernetes", "completed", result=f"{result.message}; {validation.message}")
        _set_project_status(project_id, "ready")
        return {
            **state,
            "analysis": analysis,
            "kubernetes_files": result.files,
            "validation_result": validation.model_dump(),
            "errors": errors,
            "status": "completed",
        }
    except Exception as exc:
        logger.exception("kubernetes node failed")
        _mark_step(project_id, "Kubernetes", "failed", error=str(exc))
        errors.append({"step": "kubernetes", "message": str(exc)})
        _set_project_status(project_id, "failed")
        return {**state, "errors": errors, "status": "failed"}


def after_analyze(state: DeployMindState) -> str:
    if state.get("status") == "failed":
        return "end"
    return "continue"


def after_docker(state: DeployMindState) -> str:
    if state.get("status") == "failed":
        return "end"
    return "continue"


def build_workflow():
    graph = StateGraph(DeployMindState)
    graph.add_node("analyze_repository", clone_and_analyze)
    graph.add_node("repository_intelligence", repository_intelligence_node)
    graph.add_node("run_tests", run_tests_node)
    graph.add_node("security_scan", security_scan_node)
    graph.add_node("generate_docker", generate_docker_node)
    graph.add_node("generate_kubernetes", generate_kubernetes_node)

    graph.set_entry_point("analyze_repository")
    graph.add_conditional_edges(
        "analyze_repository",
        after_analyze,
        {"continue": "repository_intelligence", "end": END},
    )
    graph.add_edge("repository_intelligence", "run_tests")
    graph.add_edge("run_tests", "security_scan")
    graph.add_edge("security_scan", "generate_docker")
    graph.add_conditional_edges(
        "generate_docker",
        after_docker,
        {"continue": "generate_kubernetes", "end": END},
    )
    graph.add_edge("generate_kubernetes", END)
    return graph.compile()


workflow = build_workflow()


@measured("analysis")
def run_analysis_workflow(project_id: int, repository_url: str, repository_name: str = "") -> DeployMindState:
    dest = Path(settings.workspace_dir) / f"project_{project_id}"
    try:
        final = workflow.invoke(
            {
                "project_id": project_id,
                "repository_url": repository_url,
                "repository_name": repository_name,
                "errors": [],
                "status": "started",
            }
        )
        return final
    finally:
        cleanup_repository(dest)
