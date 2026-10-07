"""MATLAB code the server runs in the user's MATLAB session.

Kept as text (not as package data) so that every way of installing the server carries it. The
backend writes a function to a folder of its own, puts that folder on the MATLAB path for the one
call and takes it off again: nothing is left in the base workspace."""

SAVE_LOGS_NAME = "cm_mcp_save_logs"
SAVE_LOGS = '''function out = cm_mcp_save_logs(file, model, logsFrom, fmt, wantBase, wantModel, maxBytes)
%CM_MCP_SAVE_LOGS  Save Simulink's logged data and the workspaces to a MAT file (carmaker-mcp).
%   signals          what the Simulation Data Inspector holds for the latest run of the model
%   workspace_logs   logging variables in the base workspace (signal logging, To Workspace blocks)
%   base_workspace, model_workspace   one struct per workspace, one field per variable
%   info             model, run, date
%   Returns a JSON text describing what was saved.
r = struct('run', '', 'signals', 0, 'workspace_logs', {{}}, 'base_variables', 0, ...
           'model_variables', 0, 'skipped', {{}}, 'notes', {{}});
S = struct();
info = struct('model', model, 'saved', char(datetime('now', 'Format', 'yyyy-MM-dd HH:mm:ss')), ...
              'matlab', version('-release'), 'run', '');
logNames = localLogNames(model);

if any(strcmp(logsFrom, {'inspector', 'both'}))
    run = localLatestRun(model);
    if isempty(run)
        r.notes{end+1} = 'the Simulation Data Inspector holds no run of this model';
    else
        r.run = run.Name;
        info.run = run.Name;
        if strcmp(fmt, 'struct')
            S.signals = localSignals(run);
            r.signals = numel(S.signals);
        else
            S.signals = Simulink.sdi.exportRun(run.ID);
            r.signals = S.signals.numElements;  % a vector signal is one element here
        end
    end
end

if any(strcmp(logsFrom, {'workspace', 'both'}))
    W = struct();
    for k = 1:numel(logNames)
        n = logNames{k};
        if isvarname(n) && evalin('base', sprintf('exist(''%s'', ''var'')', n)) == 1
            W.(n) = evalin('base', n);
        end
    end
    S.workspace_logs = W;
    r.workspace_logs = fieldnames(W);
    if isempty(r.workspace_logs)
        r.notes{end+1} = 'no logging variable of this model was found in the base workspace';
    end
end

if wantBase
    [S.base_workspace, skipped] = localWorkspace(evalin('base', 'whos'), ...
        @(n) evalin('base', n), maxBytes, logNames);
    r.base_variables = numel(fieldnames(S.base_workspace));
    r.skipped = [r.skipped, strcat({'base: '}, skipped)];
end
if wantModel
    mw = get_param(model, 'ModelWorkspace');
    [S.model_workspace, skipped] = localWorkspace(mw.whos, @(n) mw.getVariable(n), maxBytes, {});
    r.model_variables = numel(fieldnames(S.model_workspace));
    r.skipped = [r.skipped, strcat({'model: '}, skipped)];
end

S.info = info;
folder = fileparts(file);
if ~isempty(folder) && ~isfolder(folder)
    mkdir(folder);
end
w = whos('S');
if w.bytes > 1.8e9
    save(file, '-struct', 'S', '-v7.3');
else
    save(file, '-struct', 'S');
end
d = dir(file);
r.bytes = d.bytes;
r.variables = fieldnames(S);
out = jsonencode(r);
end


function run = localLatestRun(model)
run = [];
ids = Simulink.sdi.getAllRunIDs;
for k = numel(ids):-1:1
    c = Simulink.sdi.getRun(ids(k));
    if strcmp(c.Model, model)
        run = c;
        return
    end
end
end


function T = localSignals(run)
% Plain data, readable without Simulink: one element per signal.
T = struct('name', {}, 'time', {}, 'values', {}, 'units', {}, 'block', {});
for k = 1:run.SignalCount
    s = run.getSignalByIndex(k);
    v = s.Values;
    T(k).name = s.Name;
    T(k).time = v.Time;
    T(k).values = v.Data;
    T(k).units = char(s.Units);
    try
        T(k).block = char(s.FullBlockPath);
    catch
        T(k).block = '';
    end
end
end


function names = localLogNames(model)
% Names under which this model's logged data arrive in the base workspace.
names = {};
try
    if strcmp(get_param(model, 'ReturnWorkspaceOutputs'), 'on')
        names{end+1} = get_param(model, 'ReturnWorkspaceOutputsName');
    end
    p = {'SignalLoggingName', 'TimeSaveName', 'OutputSaveName', 'StateSaveName', ...
         'FinalStateName', 'DSMLoggingName'};
    for k = 1:numel(p)
        names{end+1} = get_param(model, p{k}); %#ok<AGROW>
    end
    old = warning('off', 'all');
    restore = onCleanup(@() warning(old));
    blocks = find_system(model, 'LookUnderMasks', 'all', 'FollowLinks', 'on', 'BlockType', 'ToWorkspace');
    for k = 1:numel(blocks)
        names{end+1} = get_param(blocks{k}, 'VariableName'); %#ok<AGROW>
    end
catch
end
names = unique(names(~cellfun(@isempty, names)), 'stable');
end


function [S, skipped] = localWorkspace(vars, getter, maxBytes, logNames)
S = struct();
skipped = {};
for k = 1:numel(vars)
    n = vars(k).name;
    c = vars(k).class;
    if strcmp(n, 'ans') || strncmp(n, 'cm_mcp_', 7)
        continue
    elseif any(strcmp(n, logNames)) || any(strcmp(c, {'Simulink.SimulationOutput', ...
            'Simulink.SimulationData.Dataset'})) || strncmp(c, 'Simulink.sdi.', 13)
        skipped{end+1} = sprintf('%s (logged data)', n); %#ok<AGROW>
    elseif strncmp(c, 'matlab.ui.', 10) || strncmp(c, 'matlab.graphics.', 16) || ...
            any(strcmp(c, {'function_handle', 'MException', 'MSLException', 'onCleanup'}))
        skipped{end+1} = sprintf('%s (%s)', n, c); %#ok<AGROW>
    elseif vars(k).bytes > maxBytes
        skipped{end+1} = sprintf('%s (%.0f MB)', n, vars(k).bytes / 1e6); %#ok<AGROW>
    else
        try
            S.(n) = getter(n);
        catch err
            skipped{end+1} = sprintf('%s (%s)', n, err.message); %#ok<AGROW>
        end
    end
end
end
'''
