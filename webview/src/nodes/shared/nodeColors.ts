export const TYPE_COLORS: Record<string, string> = {
  trigger: '#34495e',
  client: '#61affe',
  api: '#49cc90',
  entrypoint: '#16a085',
  router: '#49cc90',
  service: '#fca130',
  repository: '#9b59b6',
  database: '#e74c3c',
  external_api: '#e67e22',
  middleware: '#1abc9c',
  dependency: '#3498db',
  function: '#95a5a6',
  method: '#95a5a6',
  class: '#7f8c8d',
  exception: '#e74c3c',
  branch: '#f39c12',
  loop: '#2ecc71',
  assignment: '#2980b9',
  return: '#27ae60',
  step: '#7f8c8d',
  file: '#8e44ad',
  module: '#2c3e50',
};

export function getTypeColor(type: string): string {
  return TYPE_COLORS[type] || '#666';
}
