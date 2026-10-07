# Example sessions

What an agent does with the tools for typical requests. Names such as `My Runs/Braking` and `MyModel` stand
for your own test runs and model.

The outputs are abbreviated illustrations of the result shapes, not recordings. Which of these flows have
been run against real software is listed in [verification.md](verification.md).

## Run a test run and read the result

> "Run the braking test run and tell me the stopping distance."

```text
cm_status()                                  -> connected, idle, save_mode "collect"
cm_load_testrun("My Runs/Braking")           -> {loaded}
cm_start_sim()                               -> {started, state "preprocessing", save_mode "save"}
cm_wait_end()                                -> {finished: false, state "running", live {Time 31.2, Car.v 22.4}}
cm_wait_end()                                -> {finished: true, end_status "completed", sim_time_s 48.6,
                                                 distance_m 912.3, result_file ".../Braking_101502.erg"}
cm_results_summary(result_file, ["Car.v", "Car.ax"])
cm_results_read(result_file, ["Time", "Car.v", "Car.Distance"], t_min=40)
```

`cm_wait_end` returns after 45 s at the latest, so a long run takes several calls.

## Nothing is open yet

> "Start CarMaker and run the slalom."

```text
cm_status()                                  -> error: no shared MATLAB session
cm_session_start(model="MyModel")            -> {ready: false, waiting_for "MATLAB to start and share its engine"}
cm_session_start(model="MyModel")            -> {ready: true, steps {matlab "started", cmenv, model "opened MyModel",
                                                 gui "opened with CM_Simulink"}}
cm_load_testrun("My Runs/Slalom") ...
```

## Change a controller parameter and compare

> "Set Kp_yaw to 1.5 and compare the yaw rate with the current setting."

Controller parameters usually live in the workspace of the Simulink model, not in the base workspace.

```text
cm_list_workspace_vars(scope="MyModel", pattern="Kp*")
cm_get_workspace_var("Kp_yaw", scope="MyModel")              -> 1.0
cm_study_start("My Runs/Slalom",
    variations=[{"label": "Kp 1.0", "workspace": {"Kp_yaw": 1.0}},
                {"label": "Kp 1.5", "workspace": {"Kp_yaw": 1.5}}],
    quantities=["Car.YawRate", "Car.ay"], scope="MyModel")   -> {study "...-s1", runs 2}
cm_study_status()                                            -> {state "running", current 2, rows [...]}
cm_study_status()                                            -> {state "done", rows [two rows with statistics]}
```

The study puts `Kp_yaw` back after each run. To keep a value, set it with `cm_set_workspace_var` and persist it
with `cm_model_save`.

## Vary a vehicle parameter without touching files

> "Try the lane change with a vehicle mass of 280, 300 and 320 kg."

```text
cm_read("vehicle", "MyCar", prefix="Body.mass")              -> Body.mass = 300
cm_study_start("My Runs/LaneChange",
    variations=[{"label": "280 kg", "keys": {"Body.mass": 280}},
                {"label": "300 kg", "keys": {"Body.mass": 300}},
                {"label": "320 kg", "keys": {"Body.mass": 320}}],
    quantities=["Car.ay", "Car.Roll"])
```

`keys` are applied in memory for one run. For vehicles with CarMaker's built-in controllers the same study
runs without MATLAB with `mode="standalone"`.

## Edit a project file for good

> "Use the wet road in the braking test run."

```text
cm_read("testrun", "My Runs/Braking", prefix="Road")
cm_clone("testrun", "My Runs/Braking", "My Runs/Braking_wet")      # keep the original
cm_edit("testrun", "My Runs/Braking_wet", {"Road.FName": "Wet.rd5"})
cm_load_testrun("My Runs/Braking_wet") ...
```

Only the edited line of the file changes. `cm_changelog` shows the old value; `cm_revert_all` restores the
file and moves the clone to a trash folder.

## A run fails

> "The last run aborted. Why?"

```text
cm_wait_end()        -> {finished: true, end_status "aborted", log_errors ["ERROR PowerTrain: ... (x39)"]}
cm_log(level="error")
cm_popups()          -> {messages [{type "err", text "Cannot load ...", answer 0}]}
```

## Undo

> "Undo everything you changed."

```text
cm_changelog()
cm_revert_all()      -> {reverted [...], restored [files], moved_to_trash [created files], failed []}
```
