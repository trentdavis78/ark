# The domain module is pure data + math; keep its names readable in crash
# reports, since a wrong verdict is the failure worth diagnosing.
-keep class com.blacktop.domain.** { *; }
