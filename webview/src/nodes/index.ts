import PipelineNode from './PipelineNode';
import FunctionNode from './FunctionNode';
import LogicStepNode from './LogicStepNode';
import ResourceNode from './ResourceNode';
import CompoundNode from './CompoundNode';

export const nodeTypes = {
  pipeline: PipelineNode,
  function: FunctionNode,
  logicStep: LogicStepNode,
  resource: ResourceNode,
  compound: CompoundNode,
};
