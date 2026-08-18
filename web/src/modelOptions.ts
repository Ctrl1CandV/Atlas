import type { Provider } from './types';

export interface ModelOptions {
  llm: string[];
  agent: string[];
}

function refs(providers: Provider[]): string[] {
  return providers.flatMap((provider) => provider.models.map((model) =>
    model.includes(':') ? model : `${provider.id}:${model}`));
}

export function deriveModelOptions(providers: Provider[]): ModelOptions {
  const configured = providers.filter((provider) => provider.credential.configured);
  return {
    llm: refs(configured),
    agent: refs(configured.filter((provider) => Boolean(provider.anthropicBaseUrl))),
  };
}
