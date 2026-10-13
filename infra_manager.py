"""Provider-independent infrastructure controls for Social Watch.

Only explicitly listed application services are managed. Never add databases
to RAILWAY_MANAGED_SERVICE_IDS.
"""
import asyncio
import os
from abc import ABC, abstractmethod

import aiohttp


class InfrastructureError(RuntimeError):
    pass


class InfrastructureProvider(ABC):
    @abstractmethod
    async def run(self, action: str) -> str:
        raise NotImplementedError


class RailwayProvider(InfrastructureProvider):
    endpoint = "https://backboard.railway.com/graphql/v2"

    def __init__(self, token: str, environment_id: str, service_ids: list[str]):
        self.token = token
        self.environment_id = environment_id
        self.service_ids = service_ids

    async def _graphql(self, query: str, variables: dict) -> dict:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                json={"query": query, "variables": variables},
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 300:
                    raise InfrastructureError(f"Railway API returned HTTP {response.status}.")
                if data.get("errors"):
                    detail = str(data["errors"][0].get("message", "Railway API error"))[:240]
                    raise InfrastructureError(detail)
                return data.get("data") or {}

    async def _latest_deployment(self, service_id: str) -> dict | None:
        data = await self._graphql(
            "query ManagedService($serviceId: String!, $environmentId: String!) { "
            "serviceInstance(serviceId: $serviceId, environmentId: $environmentId) { "
            "serviceName latestDeployment { id status } } }",
            {"serviceId": service_id, "environmentId": self.environment_id},
        )
        instance = data.get("serviceInstance")
        if not instance:
            return None
        deployment = instance.get("latestDeployment")
        if deployment:
            deployment["serviceName"] = instance.get("serviceName") or service_id
        return deployment

    async def _operate_one(self, action: str, service_id: str) -> str:
        variables = {"serviceId": service_id, "environmentId": self.environment_id}
        if action == "deploy":
            await self._graphql(
                "mutation Deploy($serviceId: String!, $environmentId: String!) { "
                "serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId) }",
                variables,
            )
            return f"{service_id}: deploy requested"
        if action == "redeploy":
            await self._graphql(
                "mutation Redeploy($serviceId: String!, $environmentId: String!) { "
                "serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId) }",
                variables,
            )
            return f"{service_id}: redeploy requested"

        latest = await self._latest_deployment(service_id)
        if action == "status":
            if not latest:
                return f"{service_id}: no deployment found"
            return f"{latest.get('serviceName', service_id)}: {latest.get('status', 'unknown')}"
        if action == "start":
            await self._graphql(
                "mutation Deploy($serviceId: String!, $environmentId: String!) { "
                "serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId) }",
                variables,
            )
            return f"{service_id}: start/deploy requested"
        if not latest or not latest.get("id"):
            return f"{service_id}: skipped (no deployment ID found)"
        if action == "restart":
            await self._graphql(
                "mutation Restart($id: String!) { deploymentRestart(id: $id) }",
                {"id": latest["id"]},
            )
            return f"{latest.get('serviceName', service_id)}: restart requested"
        if action == "shutdown":
            if latest.get("status") not in {"SUCCESS", "CRASHED", "DEPLOYING", "BUILDING", "SLEEPING"}:
                return f"{latest.get('serviceName', service_id)}: skipped (state {latest.get('status', 'unknown')})"
            await self._graphql(
                "mutation Stop($id: String!) { deploymentStop(id: $id) }",
                {"id": latest["id"]},
            )
            return f"{latest.get('serviceName', service_id)}: shutdown requested"
        raise InfrastructureError(f"Unsupported action: {action}")

    async def run(self, action: str) -> str:
        if action not in {"status", "deploy", "redeploy", "restart", "shutdown", "start"}:
            raise InfrastructureError("Unsupported infrastructure action.")
        results = []
        for service_id in self.service_ids:
            try:
                results.append(await self._operate_one(action, service_id))
            except InfrastructureError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                raise InfrastructureError(f"Could not manage service {service_id} ({type(error).__name__}).") from error
        return "**Railway · " + action.upper() + "**\n" + "\n".join(f"• {item}" for item in results)


class InfrastructureManager:
    def __init__(self, provider: InfrastructureProvider | None):
        self.provider = provider

    @classmethod
    def from_environment(cls):
        provider_name = os.environ.get("INFRASTRUCTURE_PROVIDER", "railway").strip().lower()
        if provider_name == "railway":
            token = os.environ.get("RAILWAY_API_TOKEN", "").strip()
            environment_id = os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip()
            service_ids = [
                item.strip() for item in os.environ.get("RAILWAY_MANAGED_SERVICE_IDS", "").split(",")
                if item.strip()
            ]
            if not token or not environment_id or not service_ids:
                return cls(None)
            return cls(RailwayProvider(token, environment_id, service_ids))
        # New providers can be added here without changing Discord commands.
        return cls(None)

    async def run(self, action: str) -> str:
        if self.provider is None:
            raise InfrastructureError(
                "No provider is configured. Set INFRASTRUCTURE_PROVIDER=railway, "
                "RAILWAY_API_TOKEN, RAILWAY_ENVIRONMENT_ID, and "
                "RAILWAY_MANAGED_SERVICE_IDS (comma-separated application service IDs only; exclude databases)."
            )
        return await self.provider.run(action)
