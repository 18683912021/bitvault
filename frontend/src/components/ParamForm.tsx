import { Form, InputNumber, Select, Input } from 'antd';
import type { ParamSchema } from '../api/types';

// 由后端 params_schema 动态渲染参数表单。
export default function ParamForm({
  schema,
  form,
  layout = 'horizontal',
}: {
  schema: ParamSchema[];
  form: any;
  layout?: 'horizontal' | 'vertical' | 'inline';
}) {
  return (
    <Form form={form} layout={layout} initialValues={defaultsOf(schema)}>
      {schema.map((s) => (
        <Form.Item key={s.key} name={s.key} label={s.label} rules={validateOf(s)}>
          {renderField(s)}
        </Form.Item>
      ))}
    </Form>
  );
}

function defaultsOf(schema: ParamSchema[]): Record<string, any> {
  const o: Record<string, any> = {};
  for (const s of schema) o[s.key] = s.default;
  return o;
}

function validateOf(s: ParamSchema): any[] {
  const rules: any[] = [];
  if (s.type === 'int' || s.type === 'float') {
    rules.push({ required: true, message: `请输入${s.label}` });
    if (s.min != null) rules.push({ type: 'number', min: s.min, message: `不能小于 ${s.min}` });
    if (s.max != null) rules.push({ type: 'number', max: s.max, message: `不能大于 ${s.max}` });
  }
  return rules;
}

function renderField(s: ParamSchema) {
  if (s.type === 'select') {
    return (
      <Select options={s.options} placeholder={`选择${s.label}`} />
    );
  }
  if (s.type === 'int') {
    return (
      <InputNumber
        style={{ width: '100%' }}
        step={1}
        precision={0}
        min={s.min}
        max={s.max}
        placeholder={`输入${s.label}`}
      />
    );
  }
  if (s.type === 'float') {
    return (
      <InputNumber
        style={{ width: '100%' }}
        step={0.1}
        min={s.min}
        max={s.max}
        placeholder={`输入${s.label}`}
      />
    );
  }
  return <Input placeholder={`输入${s.label}`} />;
}

// 从 schema 默认值生成 params 对象
export function schemaDefaults(schema: ParamSchema[]): Record<string, any> {
  return defaultsOf(schema);
}
