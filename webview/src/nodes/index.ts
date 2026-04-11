import PipelineNode from './PipelineNode';
import FunctionNode from './FunctionNode';
import LogicStepNode from './LogicStepNode';
import ResourceNode from './ResourceNode';
import CompoundNode from './CompoundNode';
import DataFlowNode from './DataFlowNode';
import CFGBlockNode from './CFGBlockNode';
import CodeFlowNode from './CodeFlowNode';

export const nodeTypes = {
  pipeline: PipelineNode,
  function: FunctionNode,
  logicStep: LogicStepNode,
  resource: ResourceNode,
  compound: CompoundNode,
  dataFlow: DataFlowNode,
  cfgBlock: CFGBlockNode,
  codeFlow: CodeFlowNode,
};
